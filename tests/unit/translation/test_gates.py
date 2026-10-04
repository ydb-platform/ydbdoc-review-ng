"""Publication gates fail closed on source echo, mangling, missing includes."""

from __future__ import annotations

from ydbdoc_review_ng.translation.gates import (
    check_publication_gates,
    missing_relative_includes,
)


def test_cyrillic_echo_fails_en_target() -> None:
    draft = (
        "# Recovery\n\n"
        "Recovering system tablets.\n\n"
        "- При использовании конфигурации V1 необходимо обновить файл.\n"
    ).encode("utf-8")
    failures = check_publication_gates(
        draft, source_locale="ru", target_path="ydb/docs/en/core/x.md"
    )
    assert any(item.code == "source_locale_echo" for item in failures)


def test_clean_english_passes() -> None:
    draft = b"# Recovery\n\nPut the tablet into Recovery mode.\n"
    assert (
        check_publication_gates(
            draft, source_locale="ru", target_path="ydb/docs/en/core/x.md"
        )
        == ()
    )


def test_split_backtick_after_normalize_still_fails_when_present() -> None:
    draft = b"See `log`_`config` now.\n"
    failures = check_publication_gates(
        draft, source_locale="ru", target_path="ydb/docs/en/core/x.md"
    )
    assert any(item.code == "split_backtick_identifiers" for item in failures)


def test_missing_relative_include_is_reported() -> None:
    text = "{% include [check-backup](_includes/check-backup.md) %}\n"
    missing = missing_relative_includes(
        text,
        target_path="ydb/docs/en/core/recipes/backup/recovery.md",
        available_paths={"ydb/docs/en/core/recipes/backup/recovery.md"},
    )
    assert missing == ("_includes/check-backup.md",)
    failures = check_publication_gates(
        text.encode(),
        source_locale="ru",
        target_path="ydb/docs/en/core/recipes/backup/recovery.md",
        available_paths={"ydb/docs/en/core/recipes/backup/recovery.md"},
    )
    assert any(item.code == "missing_include_target" for item in failures)


def test_heading_without_blank_line_above_fails() -> None:
    draft = (
        b"**KiKiMR** is the legacy name, and so on.\n"
        b"### Tablet recovery mode {#tablet-recovery-mode}\n\n"
        b"**Recovery mode** is a restore mode.\n"
    )
    failures = check_publication_gates(
        draft, source_locale="ru", target_path="ydb/docs/en/core/concepts/glossary.md"
    )
    assert any(item.code == "heading_blank_lines" for item in failures)


def test_heading_at_start_of_file_does_not_need_blank_above() -> None:
    draft = b"### Tablet recovery mode {#tablet-recovery-mode}\n\nBody.\n"
    assert (
        check_publication_gates(
            draft, source_locale="ru", target_path="ydb/docs/en/core/concepts/glossary.md"
        )
        == ()
    )


def test_unlabeled_opening_fence_fails() -> None:
    draft = (
        b"ls /path/to/backup/snapshot/\n"
        b"```\n"
        b"manifest.json\n"
        b"```\n"
    )
    failures = check_publication_gates(
        draft,
        source_locale="ru",
        target_path="ydb/docs/en/core/recipes/backup/_includes/check-backup.md",
    )
    assert any(item.code == "unlabeled_fence_opener" for item in failures)


def test_labeled_fences_pass() -> None:
    draft = (
        b"```bash\n"
        b"ls /path/to/backup/snapshot/\n"
        b"```\n\n"
        b"```text\n"
        b"manifest.json\n"
        b"```\n"
    )
    assert (
        check_publication_gates(
            draft,
            source_locale="ru",
            target_path="ydb/docs/en/core/recipes/backup/_includes/check-backup.md",
        )
        == ()
    )


def test_dropped_opening_bash_fence_from_check_backup_fails() -> None:
    draft = (
        b"ls /path/to/backup/directory/hive/72057594037968897/"
        b"backup_20251007T193502_g214_s1222/snapshot/\n"
        b"```\n\n"
        b"```text\n"
        b"manifest.json\n"
        b"```\n"
    )
    failures = check_publication_gates(
        draft,
        source_locale="ru",
        target_path="ydb/docs/en/core/recipes/backup/_includes/check-backup.md",
    )
    assert any(item.code == "unlabeled_fence_opener" for item in failures)


def test_existing_target_include_is_not_missing() -> None:
    text = "{% include [note](_includes/tpch-dataset-note.md) %}\n"
    target = "ydb/docs/en/core/dev/optimization/structure.md"
    note = "ydb/docs/en/core/dev/optimization/_includes/tpch-dataset-note.md"
    missing = missing_relative_includes(
        text,
        target_path=target,
        available_paths={target},
        exists=lambda path: path == note,
    )
    assert missing == ()
    failures = check_publication_gates(
        text.encode(),
        source_locale="ru",
        target_path=target,
        available_paths={target},
        exists=lambda path: path == note,
    )
    assert failures == ()
