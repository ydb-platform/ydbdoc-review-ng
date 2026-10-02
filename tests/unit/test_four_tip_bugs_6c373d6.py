"""Four tip bugs vs REQUIREMENTS_RU.md (§3 / §4.1 / §5.2 / soft-publish / continue).

Reproduce on 6c373d6 before fixing. Each test fails closed until the hole is closed.
"""

from __future__ import annotations

import json

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.models import AttemptError, ModelCallResult
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict
from ydbdoc_review_ng.scope import FileOperation
from ydbdoc_review_ng.translation_plan import (
    PlanAction,
    build_translation_plan,
    reconcile_candidate_outputs,
    reconcile_fixed_outputs,
)
from tests.unit.test_translation_plan import (
    ROOTS,
    change,
    entry,
    inventory,
    manifest,
    toc_postcondition,
    toc_source_snapshots,
)


def test_bug1_missing_required_toc_must_soft_pending_not_uncovered() -> None:
    """§4.1/§5.2: deleted required target TOC → null for critic, not toc_uncovered."""
    document = entry("manual/page.md")
    # Soft-pending postcondition (what verify must set when branch TOC is gone).
    plan = build_translation_plan(
        inventory(
            change(document.pair.source_path.value),
            change("ydb/docs/ru/core/manual/toc_i.yaml"),
        ),
        ROOTS,
        manifest(document),
        toc_postconditions={RepoPath("ydb/docs/en/core/manual/toc_i.yaml"): None},
        toc_source_snapshots=toc_source_snapshots(
            "manual/toc_i.yaml", ("Page", "page.md")
        ),
    )
    toc = next(item for item in plan.inputs if item.action is PlanAction.SYNC_TOC)
    assert toc.expected_sha256 is None
    reconcile_fixed_outputs(
        plan,
        (
            (document.pair.target_path.value, b"# Page\n"),
            ("ydb/docs/en/core/manual/toc_i.yaml", None),
        ),
    )


def test_bug1_verify_must_null_toc_postcondition_when_branch_toc_missing() -> None:
    """Hole: verify nulls files[] but leaves toc_postconditions expected → uncovered."""
    import inspect

    from ydbdoc_review_ng import runtime_content as content_mod

    source = inspect.getsource(content_mod.RuntimeContent.select_source)
    # After nulling missing TOC for critic, postcondition must also go soft-pending.
    assert "toc_postconditions[" in source or "toc_postconditions =" in source
    assert "actual is None and expected is not None" in source
    # The missing-TOC branch must clear the postcondition, not only files[].
    missing_branch = source.split("actual is None and expected is not None", 1)[1][:400]
    assert "toc_postconditions" in missing_branch


def test_bug2_critic_malformed_yaml_toc_soft_publishes_via_reconcile() -> None:
    """§2/§7 soft-publish: assembled UTF-8 critic TOC must not die on YAML parse."""
    document = entry("page.md")
    original = b"items:\n- name: Source\n  href: page.md\n"
    plan = build_translation_plan(
        inventory(
            change(document.pair.source_path.value),
            change(ROOTS.ru.value + "/toc.yaml"),
        ),
        ROOTS,
        manifest(document),
        toc_postconditions=toc_postcondition("toc.yaml", original),
        toc_source_snapshots=toc_source_snapshots("toc.yaml", ("Source", "page.md")),
    )
    bad = b"items: [\n  - name: Broken UTF-8 still assembled\n"
    # Must NOT raise — diagnostics ≠ gate; arbiter must see critic bytes.
    reconcile_candidate_outputs(
        plan,
        (
            (document.pair.target_path.value, b"# Page\n"),
            (ROOTS.en.value + "/toc.yaml", bad),
        ),
        toc_postconditions=toc_postcondition("toc.yaml", original),
    )


def test_bug2_review_pr_keeps_malformed_critic_toc_for_arbiter() -> None:
    """Production path: RuntimeContent._validate_toc_correction soft-keeps critic TOC."""
    from ydbdoc_review_ng.domain import GitSha, RepositoryId, SnapshotRef
    from ydbdoc_review_ng.runtime_content import RuntimeContent
    from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError
    from ydbdoc_review_ng.runtime_metadata import _toc

    toc = "ydb/docs/en/core/toc.yaml"
    page = "ydb/docs/en/core/page.md"
    draft = b"items:\n- name: Draft\n  href: page.md\n"
    bad = "items: [\n  - name: Critic fixed labels\n"
    page_text = "# Page\n"

    # Production `_toc` raises RuntimeBoundaryError — the soft path must catch it.
    with __import__("pytest").raises(RuntimeBoundaryError):
        _toc(bad.encode(), "translation_plan_toc_correction_invalid")

    class Models:
        def __init__(self) -> None:
            self.payloads = [
                json.dumps({"files": {page: page_text}}),
                json.dumps({"files": {toc: bad}}),
                json.dumps({"verdict": "GREEN", "findings": []}),
                json.dumps({"verdict": "GREEN", "findings": []}),
            ]

        def invoke(self, request):
            return ModelCallResult(self.payloads.pop(0), None, ())

    seen: list[dict[str, bytes]] = []

    def validate(files: dict[str, bytes]) -> None:
        seen.append(dict(files))
        if toc not in files:
            return
        # Call the real production soft-validator (not a fake that catches RBE).
        RuntimeContent._validate_toc_correction(
            SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40)),
            RepoPath(toc),
            draft,
            files[toc],
        )
        reconcile_candidate_outputs(
            build_translation_plan(
                inventory(
                    change("ydb/docs/ru/core/page.md"),
                    change("ydb/docs/ru/core/toc.yaml"),
                ),
                ROOTS,
                manifest(entry("page.md")),
                toc_postconditions=toc_postcondition("toc.yaml", draft),
                toc_source_snapshots=toc_source_snapshots("toc.yaml", ("Draft", "page.md")),
            ),
            (
                (page, page_text.encode()),
                (toc, files[toc]),
            ),
            toc_postconditions=toc_postcondition("toc.yaml", draft),
        )

    corrected, final = review_pr(
        Models(),
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={
            "ydb/docs/ru/core/page.md": b"# Page\n",
            "ydb/docs/ru/core/toc.yaml": b"items:\n- name: Page\n  href: page.md\n",
        },
        translated_files={
            page: page_text.encode(),
            toc: draft,
        },
        glossary_files={},
        validate_files=validate,
    )
    assert seen, "critic correction must pass validate once"
    assert corrected[toc] == bad.encode()
    assert final.verdict is Verdict.GREEN


def test_bug3_delete_only_non_final_finding_must_be_checkpointable() -> None:
    """§4/§5.3: delete-only NON_FINAL → RED finding path must be capture-reviewable."""
    document = entry("page.md", FileOperation.DELETE_TARGET)
    plan = build_translation_plan(
        inventory(change(document.pair.source_path.value, "removed")),
        ROOTS,
        manifest(document),
    )
    fixed_files = ((document.pair.target_path.value, None),)
    documents: tuple = ()
    reviewable = {doc.entry.pair.target_path for doc in documents} | {
        RepoPath(path) for path, _value in fixed_files
    } | {
        item.target_path
        for item in plan.inputs
        if item.target_path is not None
    } | {RepoPath("resource-review")}

    models = _Scripted(
        [
            json.dumps({"files": {}}),
            ModelCallResult(None, AttemptError.NON_FINAL, ()),
        ]
    )
    _corrected, final = review_pr(
        models,
        critic_model="c",
        arbiter_model="a",
        source_files={},
        translated_files={},
        glossary_files={},
        validate_files=lambda files: None,
        binary_manifest={},
    )
    assert final.verdict is Verdict.RED
    finding_paths = {RepoPath(item.target_path) for item in final.findings}
    assert finding_paths, "NON_FINAL must produce a finding"
    assert finding_paths <= reviewable, (
        f"checkpoint path_mismatch: {finding_paths - reviewable} not reviewable"
    )


def test_bug3_continue_reviewable_must_admit_resource_review_marker() -> None:
    """Hole: capture admits resource-review; continue reviewable must too (#17 residual)."""
    import inspect

    from ydbdoc_review_ng import runtime_continue as continue_mod

    source = inspect.getsource(continue_mod.replay_continue)
    assert 'RepoPath("resource-review")' in source or "resource-review" in source


def test_bug4_source_toc_removed_plans_delete_target() -> None:
    """§1.2/§3: full source TOC Git delete → DELETE_TARGET for paired TOC."""
    document = entry("page.md", FileOperation.DELETE_TARGET)
    plan = build_translation_plan(
        inventory(
            change(document.pair.source_path.value, "removed"),
            change("ydb/docs/ru/core/manual/toc_i.yaml", "removed"),
        ),
        ROOTS,
        manifest(document),
    )
    toc = next(
        item
        for item in plan.inputs
        if item.change.path.value.endswith("toc_i.yaml")
    )
    assert toc.action is PlanAction.DELETE_TARGET
    assert toc.target_path == RepoPath("ydb/docs/en/core/manual/toc_i.yaml")
    reconcile_fixed_outputs(
        plan,
        (
            (document.pair.target_path.value, None),
            ("ydb/docs/en/core/manual/toc_i.yaml", None),
        ),
    )


def test_bug4_toc_only_removed_plans_delete_without_markdown() -> None:
    """TOC-only delete must mirror even when Markdown inventory is empty."""
    plan = build_translation_plan(
        inventory(change("ydb/docs/ru/core/manual/toc_i.yaml", "removed")),
        ROOTS,
        manifest(),  # empty Markdown scope; direction still RU→EN
    )
    assert len(plan.inputs) == 1
    assert plan.inputs[0].action is PlanAction.DELETE_TARGET
    assert plan.inputs[0].target_path == RepoPath("ydb/docs/en/core/manual/toc_i.yaml")
    reconcile_fixed_outputs(plan, (("ydb/docs/en/core/manual/toc_i.yaml", None),))


class _Scripted:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = list(payloads)

    def invoke(self, request):
        item = self.payloads.pop(0)
        if isinstance(item, ModelCallResult):
            return item
        return ModelCallResult(item, None, ())
