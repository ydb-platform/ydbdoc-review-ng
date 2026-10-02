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


def test_build_maps_cli_flags_and_short_allcaps_from_old_en_backticks() -> None:
    old_en = (
        b"* `--name` unique\n"
        b"* `--hive-id=N` hive\n"
        b"* `NEW` waiting\n"
        b"* `WORKING` ready\n"
        b"* format `gen:counter => collect_gen:collect_step` barrier\n"
    )
    styles = build_presentation_map(old_en, source_snapshot=SNAPSHOT, source_path=PATH)
    assert styles["--name"].inline_code is True
    assert styles["--hive-id=N"].inline_code is True
    assert styles["NEW"].inline_code is True
    assert styles["WORKING"].inline_code is True
    assert styles["gen:counter"].inline_code is True
    assert styles["collect_gen:collect_step"].inline_code is True


def test_apply_wraps_cli_flags_short_states_and_colon_tokens() -> None:
    old_en = (
        b"* `--name` unique\n"
        b"* `NEW` waiting\n"
        b"* `WORKING` ready\n"
        b"* format `gen:counter => collect_gen:collect_step` barrier\n"
    )
    styles = build_presentation_map(old_en, source_snapshot=SNAPSHOT, source_path=PATH)
    draft = (
        b"* --name unique\n"
        b"* NEW waiting\n"
        b"* WORKING ready\n"
        b"* format gen:counter => collect\\_gen:collect\\_step barrier\n"
    )
    applied = _apply(draft, styles)
    assert b"* `--name` unique\n" in applied
    assert b"* `NEW` waiting\n" in applied
    assert b"* `WORKING` ready\n" in applied
    assert b"`gen:counter`" in applied
    assert b"`collect_gen:collect_step`" in applied
    assert b"collect\\_gen" not in applied
