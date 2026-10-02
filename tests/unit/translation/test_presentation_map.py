"""P0: old-EN presentation map is optional formatting transfer only."""

from __future__ import annotations

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.translation.presentation import (
    apply_presentation_map,
    build_presentation_map,
)

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/en/test.md")


def _apply(draft: bytes, styles) -> bytes:
    return apply_presentation_map(
        draft, styles, source_snapshot=SNAPSHOT, source_path=PATH
    )


def test_absent_old_en_build_is_empty_and_apply_is_noop() -> None:
    draft = b"See BS_CONTROLLER and CREATE_FAILED.\n"
    styles = build_presentation_map(None, source_snapshot=SNAPSHOT, source_path=PATH)
    assert styles == {}
    assert _apply(draft, styles) == draft


def test_build_marks_backticked_and_bare_identifiers() -> None:
    old_en = b"Use `BS_CONTROLLER` and CREATE_FAILED with `POOL_NAME`.\n"
    styles = build_presentation_map(old_en, source_snapshot=SNAPSHOT, source_path=PATH)
    assert styles["BS_CONTROLLER"].inline_code is True
    assert styles["POOL_NAME"].inline_code is True
    assert styles["CREATE_FAILED"].inline_code is False


def test_apply_wraps_bare_atoms_that_were_inline_code_in_old_en() -> None:
    old_en = b"Use `BS_CONTROLLER` and CREATE_FAILED.\n"
    styles = build_presentation_map(old_en, source_snapshot=SNAPSHOT, source_path=PATH)
    draft = b"See BS_CONTROLLER and CREATE_FAILED.\n"
    assert _apply(draft, styles) == b"See `BS_CONTROLLER` and CREATE_FAILED.\n"


def test_apply_does_not_double_wrap_existing_inline_code() -> None:
    old_en = b"Use `BS_CONTROLLER`.\n"
    styles = build_presentation_map(old_en, source_snapshot=SNAPSHOT, source_path=PATH)
    draft = b"See `BS_CONTROLLER` now.\n"
    assert _apply(draft, styles) == draft


def test_apply_prefers_unescaped_forms_from_old_en() -> None:
    old_en = b"Status CREATE_FAILED.\n"
    styles = build_presentation_map(old_en, source_snapshot=SNAPSHOT, source_path=PATH)
    draft = b"Status CREATE\\_FAILED.\n"
    assert _apply(draft, styles) == b"Status CREATE_FAILED.\n"
