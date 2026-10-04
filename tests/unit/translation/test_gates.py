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
