"""Complete review input bytes come only from the frozen PR inventory/candidate."""

from dataclasses import replace
from typing import cast

import pytest

from ydbdoc_review_ng.application import ImmutableRunSnapshot, WorkflowCandidate
from ydbdoc_review_ng.continuation import SourceChange, SourceChangeInventory
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import GitSha, Mode, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import (
    FrozenPreparation,
    FrozenSourcePlans,
    RuntimeContent,
    pack,
)
from ydbdoc_review_ng.runtime_github import GitHubBackend
from ydbdoc_review_ng.scope import PotentialScopeSet, ScopeManifest
from ydbdoc_review_ng.translation_plan import TranslationPlan

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

    source_files, _, _ = content._pr_review_inputs(candidate)

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

    source_files, translated_files, _ = content._pr_review_inputs(candidate)

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

    source_files, translated_files, _ = content._pr_review_inputs(candidate)

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

    _, _, glossary_files = content._pr_review_inputs(
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

    source_files, translated_files, glossary_files = content._pr_review_inputs(candidate)

    assert source_files == {
        RU + "page.md": b"# Current\n\n[Dependency](dependency.md)\n",
        RU + "complete.md": b"# Source already paired in source PR\n",
    }
    assert translated_files == {EN + "page.md": b"# Current translation\n"}
    assert glossary_files == {}
    assert (CURRENT, RU + "dependency.md") not in reader.reads
    assert (TARGET, EN + "complete.md") not in reader.reads
