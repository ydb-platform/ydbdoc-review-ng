"""Complete review input bytes come only from the frozen PR inventory/candidate."""

import json
from dataclasses import replace
from typing import cast

import pytest

from tests.unit.quality.test_pr_review import FifoModels, prompt_map
from ydbdoc_review_ng.application import ImmutableRunSnapshot, WorkflowCandidate
from ydbdoc_review_ng.continuation import SourceChange, SourceChangeInventory
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import (
    FilePair,
    GitSha,
    Locale,
    Mode,
    ModelRole,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import PairKey
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.publication import FileChange, PublicationPlan
from ydbdoc_review_ng.quality import QualityInputError, Verdict
from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import (
    Document,
    FrozenPreparation,
    FrozenSourcePlans,
    RuntimeContent,
    pack,
    unpack,
)
from ydbdoc_review_ng.runtime_github import GitHubBackend
from ydbdoc_review_ng.scope import (
    FileOperation,
    PotentialScopeSet,
    ScopeEntry,
    ScopeManifest,
    ScopeOrigin,
)
from ydbdoc_review_ng.translation import build_translation_request
from ydbdoc_review_ng.translation_plan import (
    TranslationPlan,
    TranslationPlanError,
    build_translation_plan,
)

REPOSITORY = RepositoryId("ydb-platform/ydb")
CURRENT = SnapshotRef(REPOSITORY, GitSha("a" * 40))
PREIMAGE = SnapshotRef(REPOSITORY, GitSha("b" * 40))
PR_CHANGE = SnapshotRef(REPOSITORY, GitSha("c" * 40))
TARGET = SnapshotRef(REPOSITORY, GitSha("e" * 40))
RU = "ydb/docs/ru/core/"
EN = "ydb/docs/en/core/"


class PinnedReader:
    def __init__(self, files: dict[tuple[SnapshotRef, str], bytes]) -> None:
        self.files = files
        self.reads: list[tuple[SnapshotRef, str]] = []

    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath) -> bytes | None:
        self.reads.append((snapshot, path.value))
        return self.files.get((snapshot, path.value))


def frozen_content(
    files: dict[tuple[SnapshotRef, str], bytes],
    changes: dict[str, str],
    *,
    direction: Direction = Direction.RU_TO_EN,
) -> tuple[RuntimeContent, PinnedReader]:
    reader = PinnedReader(files)
    source = RuntimeSource({}, cast(GitHubBackend, reader))
    source.snapshots = ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        PR_CHANGE,
        CURRENT,
        CURRENT,
        CURRENT,
        CURRENT,
        CURRENT,
        PR_CHANGE,
    )
    source.source_base_snapshot = PREIMAGE
    source.source_change_snapshot = PR_CHANGE
    source.metadata_snapshot = TARGET
    source.inventory = SourceChangeInventory(
        tuple(
            SourceChange(RepoPath(path), status, None, None)
            for path, status in sorted(changes.items())
        )
    )
    # No model or mutation methods exist on these read-only boundaries.
    content = RuntimeContent(source, cast(RecordedModels, object()), {})
    preparation = FrozenPreparation(
        ImmutableRunSnapshot(
            Mode.DOC_VERIFY, CURRENT.commit_sha, TARGET.commit_sha, "translation", None
        ),
        source.snapshots,
        source.inventory,
        TARGET,
        (),
        PotentialScopeSet(CURRENT, content.roots, (), None),
        False,
    )
    content.plans = FrozenSourcePlans(
        preparation,
        ScopeManifest(direction, CURRENT, content.roots, (), (), 0, 0),
        (),
        (),
        TranslationPlan(direction, (), ()),
    )
    return content, reader


def review_fixture():
    source = b"# BlobDepot\n\nUse `BlobDepot`.\n"
    original = b"# Depot\n\nUse `BlobDepot`.\n"
    content, _reader = frozen_content(
        {(CURRENT, RU + name): source for name in ("a.md", "b.md")},
        {RU + "a.md": "modified", RU + "b.md": "modified"},
    )
    documents = []
    for name in ("a.md", "b.md"):
        pair = FilePair(Locale.RU, Locale.EN, RepoPath(RU + name), RepoPath(EN + name))
        entry = ScopeEntry(
            pair,
            source,
            original,
            ScopeOrigin.INITIAL,
            FileOperation.TRANSLATE,
            (PairKey(RepoPath(name)),),
            None,
            None,
        )
        plan = build_markdown_plan(CURRENT, pair.source_path, source)
        documents.append(Document(entry, source, plan, build_translation_request(source, plan)))
    content.documents = tuple(documents)
    content.plans = replace(content.plans, documents=tuple(documents))
    candidate = WorkflowCandidate(pack({EN + name: original for name in ("a.md", "b.md")}), None)
    return content, candidate


@pytest.mark.parametrize("files", [{}, {EN + "removed.md": None}])
def test_empty_review_still_invokes_critic_and_arbiter(files):
    """REQUIREMENTS §4.1: zero text pairs → critic {files:{}} + arbiter (#15)."""
    content, _reader = frozen_content({}, {RU + "removed.md": "removed"})
    models = FifoModels(
        [
            json.dumps({"files": {}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )
    content.models = models
    candidate = WorkflowCandidate(pack(files), None)

    result = content.review(content.plans.preparation.snapshot, candidate)

    assert [call.role.value for call in models.calls] == ["critic", "arbiter"]
    assert result.final.verdict is Verdict.GREEN
    assert result.final.findings == ()


def test_empty_review_without_manifest_stays_noop():
    content, _reader = frozen_content({}, {RU + "removed.md": "removed"})
    content.plans = replace(content.plans, manifest=None)
    models = FifoModels([])
    content.models = models
    candidate = WorkflowCandidate(pack({}), None)

    result = content.review(content.plans.preparation.snapshot, candidate)

    assert models.calls == []
    assert result.final.verdict is Verdict.GREEN
    assert result.accepted_maps == ()


@pytest.mark.parametrize("verdict", ["GREEN", "YELLOW", "RED"])
def test_runtime_reviews_all_files_once_and_preserves_arbiter_verdict(verdict):
    content, candidate = review_fixture()
    content.environment = {"YDBDOC_MAX_CRITIC_REQUEST_CHARACTERS": "1"}
    content.review_paths = (RepoPath(EN + "b.md"),)
    corrected = {EN + name: "# BlobDepot\n\nUse `BlobDepot`.\n" for name in ("a.md", "b.md")}
    models = FifoModels(
        [
            json.dumps({"files": corrected}),
            json.dumps(
                {
                    "verdict": verdict,
                    "findings": []
                    if verdict == "GREEN"
                    else [
                        {
                            "target_path": EN + "b.md",
                            "target_line": 3,
                            "searchable_snippet": "Use `BlobDepot`.",
                            "reason": "Residual terminology issue.",
                            "expected_correction": "Clarify the intended component meaning.",
                        }
                    ],
                }
            ),
        ]
    )
    content.models = models
    result = content.review(content.plans.preparation.snapshot, candidate)
    assert unpack(result.final_candidate) == {
        path: text.encode() for path, text in corrected.items()
    }
    assert result.final.verdict is Verdict(verdict)
    assert result.repair_applied
    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.ARBITER]
    assert prompt_map(models.calls[1], "translation-pr-files") == corrected
    assert tuple(item.target_path.value for item in result.accepted_maps) == (
        EN + "a.md",
        EN + "b.md",
    )


@pytest.mark.parametrize("has_document_plans", [True, False])
def test_invalid_second_file_retries_then_passes_draft_to_arbiter(has_document_plans):
    """REQUIREMENTS §4.1: after one critic retry, files go to arbiter as-is."""
    content, candidate = review_fixture()
    if not has_document_plans:
        content.documents = ()
    # Missing required path is a hard contract failure (not a soft §2 diagnostic).
    incomplete_files = {
        EN + "a.md": "# BlobDepot\n\nUse `BlobDepot`.\n",
    }
    models = FifoModels(
        [
            json.dumps({"files": incomplete_files}),
            json.dumps({"files": incomplete_files}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )
    content.models = models
    result = content.review(content.plans.preparation.snapshot, candidate)
    assert [call.role.value for call in models.calls] == ["critic", "critic", "arbiter"]
    assert result.final.verdict is Verdict.GREEN
    # Draft bytes preserved when critic corrections remain invalid.
    assert unpack(result.final_candidate)[EN + "b.md"] == b"# Depot\n\nUse `BlobDepot`.\n"


@pytest.mark.parametrize("invalid_change", [None, "href"])
def test_runtime_applies_complete_toc_including_href_corrections(invalid_change):
    """REQUIREMENTS §3.6/§4.1: critic may rewrite TOC href/hierarchy (#7)."""
    from tests.unit.test_translation_plan import (
        change,
        entry,
        inventory,
        manifest,
        toc_source_snapshots,
    )

    content, candidate = review_fixture()
    label = "Depot `BlobDepot` [reference](ref.md) {{version}}"
    toc = (
        "items:\n- name: " + label + "\n  href: a.md\n- name: Target only\n  href: extra.md\n"
    ).encode()
    source_toc = b"items:\n- name: BlobDepot\n  href: a.md\n"
    content.source.github.files[(CURRENT, RU + "toc.yaml")] = source_toc
    preparation = replace(
        content.plans.preparation,
        inventory=SourceChangeInventory(
            tuple(
                sorted(
                    (
                        *content.plans.preparation.inventory.files,
                        SourceChange(RepoPath(RU + "toc.yaml"), "modified", None, None),
                    ),
                    key=lambda item: item.path.value,
                )
            )
        ),
    )
    plan = build_translation_plan(
        inventory(change(RU + "a.md"), change(RU + "b.md"), change(RU + "toc.yaml")),
        content.roots,
        manifest(entry("a.md"), entry("b.md")),
        toc_postconditions={RepoPath(EN + "toc.yaml"): toc},
        toc_source_snapshots=toc_source_snapshots("toc.yaml", ("BlobDepot", "a.md")),
    )
    content.plans = replace(
        content.plans,
        preparation=preparation,
        fixed_files=((EN + "toc.yaml", toc),),
        translation_plan=plan,
    )
    candidate = WorkflowCandidate(pack({**unpack(candidate.content), EN + "toc.yaml": toc}), None)
    corrected = {EN + name: "# BlobDepot\n\nUse `BlobDepot`.\n" for name in ("a.md", "b.md")}
    corrected[EN + "toc.yaml"] = (
        "items:\n- name: Correct "
        + label
        + "\n  href: "
        + ("wrong.md" if invalid_change == "href" else "a.md")
        + "\n- name: Target only\n  href: extra.md\n"
    )
    models = FifoModels([json.dumps({"files": corrected}), '{"verdict":"GREEN","findings":[]}'])
    content.models = models
    result = content.review(preparation.snapshot, candidate)
    assert prompt_map(models.calls[1], "translation-pr-files") == corrected
    final_files = {path: text.encode() for path, text in corrected.items()}
    assert unpack(result.final_candidate) == final_files


def test_pr_inputs_use_current_pinned_source_not_diff_or_preimage() -> None:
    current = "# BlobDepot\n\nТекущий текст.\n\nНеизменённый абзац.\n".encode()
    content, reader = frozen_content(
        {
            (CURRENT, RU + "page.md"): current,
            (PREIMAGE, RU + "page.md"): b"# Before the PR\n",
            (PR_CHANGE, RU + "page.md"): b"# Historical merged PR head\n",
        },
        {RU + "page.md": "modified"},
    )
    candidate = WorkflowCandidate(pack({EN + "page.md": b"# Current translation\n"}), None)

    source_files, _, _, _, _ = content._pr_review_inputs(candidate)

    assert source_files == {
        RU + "page.md": "# BlobDepot\n\nТекущий текст.\n\nНеизменённый абзац.\n".encode()
    }
    reader.files[(PREIMAGE, RU + "page.md")] = b"# A different preimage\n"
    reader.files[(PR_CHANGE, RU + "page.md")] = b"# A different merge snapshot\n"
    moving_head = SnapshotRef(REPOSITORY, GitSha("f" * 40))
    reader.files[(moving_head, RU + "page.md")] = b"# A moving branch\n"
    content.source.inventory = SourceChangeInventory(())
    content.source.snapshots = replace(
        content.source.snapshots,
        source_snapshot=moving_head,
        target_snapshot=moving_head,
        scope_snapshot=moving_head,
        translation_base_snapshot=moving_head,
        merge_base_with=moving_head,
    )
    assert content._pr_review_inputs(candidate)[0] == source_files
    assert all(snapshot not in {PREIMAGE, PR_CHANGE, moving_head} for snapshot, _ in reader.reads)


def test_pr_inputs_include_complete_translation_files_and_index() -> None:
    content, _ = frozen_content(
        {
            (CURRENT, RU + "page.md"): b"# BlobDepot\n\nCurrent.\n\nUnchanged.\n",
            (CURRENT, RU + "index.md"): b"# Index\n\n[BlobDepot](page.md)\n",
            (TARGET, EN + "page.md"): b"# Target base must not replace the candidate\n",
            (TARGET, EN + "index.md"): b"# Old index\n",
        },
        {RU + "page.md": "modified", RU + "index.md": "modified"},
    )
    candidate = WorkflowCandidate(
        pack(
            {
                EN
                + "page.md": b"# BlobDepot\n\nCurrent translation.\n\nUnchanged target paragraph.\n",
                EN + "index.md": b"# Documentation\n\n[BlobDepot](page.md)\n",
            }
        ),
        None,
    )

    source_files, translated_files, _, _, _ = content._pr_review_inputs(candidate)

    assert source_files == {
        RU + "page.md": b"# BlobDepot\n\nCurrent.\n\nUnchanged.\n",
        RU + "index.md": b"# Index\n\n[BlobDepot](page.md)\n",
    }
    assert translated_files == {
        EN + "page.md": b"# BlobDepot\n\nCurrent translation.\n\nUnchanged target paragraph.\n",
        EN + "index.md": b"# Documentation\n\n[BlobDepot](page.md)\n",
    }


@pytest.mark.parametrize("direction", [Direction.RU_TO_EN, Direction.EN_TO_RU])
@pytest.mark.parametrize("toc_name", ["toc.yaml", "toc_i.yaml", "toc_p.yml"])
def test_pr_inputs_include_current_source_toc_and_target_toc(direction, toc_name) -> None:
    source_root, target_root = (RU, EN) if direction is Direction.RU_TO_EN else (EN, RU)
    content, _ = frozen_content(
        {
            (CURRENT, source_root + toc_name): b"items:\n- name: BlobDepot\n  href: page.md\n",
            (PR_CHANGE, source_root + toc_name): b"items: []\n",
            (TARGET, target_root + toc_name): b"items: []\n",
        },
        {source_root + toc_name: "modified"},
        direction=direction,
    )
    candidate = WorkflowCandidate(
        pack(
            {
                target_root + toc_name: (
                    b"items:\n- name: BlobDepot\n  href: page.md\n"
                    b"- name: Target-only entry\n  href: target-only.md\n"
                )
            }
        ),
        None,
    )

    source_files, translated_files, _, _, _ = content._pr_review_inputs(candidate)

    assert source_files == {source_root + toc_name: b"items:\n- name: BlobDepot\n  href: page.md\n"}
    assert translated_files == {
        target_root + toc_name: (
            b"items:\n- name: BlobDepot\n  href: page.md\n"
            b"- name: Target-only entry\n  href: target-only.md\n"
        )
    }


def test_pr_inputs_include_whole_bilingual_glossary() -> None:
    ru_glossary = (
        "# Словарь\n\n" + "Определение.\n" * 900 + "## Хвост\n\nНеупомянутый термин.\n"
    ).encode()
    en_glossary = (
        "# Glossary\n\n" + "Definition.\n" * 900 + "## Tail\n\nUnmentioned term.\n"
    ).encode()
    content, _ = frozen_content(
        {
            (CURRENT, RU + "page.md"): b"# No glossary terms here\n",
            (CURRENT, RU + "concepts/glossary.md"): ru_glossary,
            (TARGET, EN + "concepts/glossary.md"): en_glossary,
            (PREIMAGE, RU + "concepts/glossary.md"): b"# Wrong source glossary\n",
            (CURRENT, EN + "concepts/glossary.md"): b"# Wrong target glossary\n",
        },
        {RU + "page.md": "modified"},
    )
    content.environment = {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "10"}

    _, _, glossary_files, _, _ = content._pr_review_inputs(
        WorkflowCandidate(pack({EN + "page.md": b"# Complete current candidate\n"}), None)
    )

    assert glossary_files == {
        RU + "concepts/glossary.md": (
            "# Словарь\n\n" + "Определение.\n" * 900 + "## Хвост\n\nНеупомянутый термин.\n"
        ).encode(),
        EN + "concepts/glossary.md": (
            "# Glossary\n\n" + "Definition.\n" * 900 + "## Tail\n\nUnmentioned term.\n"
        ).encode(),
    }


def test_pr_inputs_exclude_files_outside_the_actual_corresponding_pr_sets() -> None:
    content, reader = frozen_content(
        {
            (CURRENT, RU + "page.md"): b"# Current\n\n[Dependency](dependency.md)\n",
            (CURRENT, RU + "dependency.md"): b"# Dependency outside source PR\n",
            (CURRENT, RU + "complete.md"): b"# Source already paired in source PR\n",
            (CURRENT, RU + "removed.md"): b"# Recreated since historical PR deletion\n",
            (CURRENT, RU + "image.png"): b"\x89PNG\x00\xff",
            (CURRENT, "README.md"): b"# Outside locale\n",
            (TARGET, EN + "complete.md"): b"# Existing target outside translation PR\n",
            (TARGET, EN + "tombstone.md"): b"# Old target for absent source\n",
        },
        {
            RU + "page.md": "modified",
            RU + "complete.md": "modified",
            EN + "complete.md": "modified",
            RU + "removed.md": "removed",
            RU + "tombstone.md": "modified",
            RU + "image.png": "added",
            "README.md": "modified",
        },
    )
    candidate = WorkflowCandidate(
        pack(
            {
                EN + "page.md": b"# Current translation\n",
                EN + "dependency.md": b"# A translated dependency\n",
                EN + "removed.md": None,
                EN + "tombstone.md": None,
                EN + "image.png": b"\x89PNG\x00\xff",
                "README.md": b"# Outside locale\n",
            }
        ),
        None,
    )

    source_files, translated_files, glossary_files, _, _ = content._pr_review_inputs(candidate)

    assert source_files == {
        RU + "page.md": b"# Current\n\n[Dependency](dependency.md)\n",
        RU + "complete.md": b"# Source already paired in source PR\n",
    }
    assert translated_files == {EN + "page.md": b"# Current translation\n"}
    assert glossary_files == {}
    assert (CURRENT, RU + "dependency.md") not in reader.reads
    assert (TARGET, EN + "complete.md") not in reader.reads
