"""#18: doc_continue must not break scope_sha256 via TOC retranslation."""

from __future__ import annotations

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.translation_plan import PlanAction, build_translation_plan, translation_plan_sha256
from tests.unit.test_translation_plan import (
    ROOTS,
    change,
    entry,
    inventory,
    manifest,
    toc_postcondition,
    toc_source_snapshots,
)


def test_toc_scope_hash_ignores_translated_wording() -> None:
    """§5 continue: TOC expected wording must not flip scope_sha256."""
    document = entry("page.md")
    source_toc = "toc.yaml"
    plan_a = build_translation_plan(
        inventory(
            change(document.pair.source_path.value),
            change("ydb/docs/ru/core/toc.yaml"),
        ),
        ROOTS,
        manifest(document),
        toc_postconditions=toc_postcondition(
            source_toc, b"items:\n- name: New EN\n  href: page.md\n"
        ),
        toc_source_snapshots=toc_source_snapshots(source_toc, ("New", "page.md")),
    )
    plan_b = build_translation_plan(
        inventory(
            change(document.pair.source_path.value),
            change("ydb/docs/ru/core/toc.yaml"),
        ),
        ROOTS,
        manifest(document),
        toc_postconditions=toc_postcondition(
            source_toc, b"items:\n- name: Another valid EN\n  href: page.md\n"
        ),
        toc_source_snapshots=toc_source_snapshots(source_toc, ("New", "page.md")),
    )
    assert translation_plan_sha256(plan_a) == translation_plan_sha256(plan_b)
    toc_a = next(item for item in plan_a.inputs if item.action is PlanAction.SYNC_TOC)
    toc_b = next(item for item in plan_b.inputs if item.action is PlanAction.SYNC_TOC)
    assert toc_a.expected_sha256 != toc_b.expected_sha256
