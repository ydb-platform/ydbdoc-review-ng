"""Publication checks protect technical content without enforcing prose style."""

from __future__ import annotations

from ydbdoc_review_ng.translation.gates import (
    check_publication_gates,
    join_soft_wrapped_prose,
    missing_relative_includes,
)


def test_cyrillic_text_is_left_to_semantic_review() -> None:
    draft = (
        "# Recovery\n\n"
        "Recovering system tablets.\n\n"
        "- При использовании конфигурации V1 необходимо обновить файл.\n"
    ).encode()
    failures = check_publication_gates(
        draft, source_locale="ru", target_path="ydb/docs/en/core/x.md"
    )
    assert failures == ()


def test_clean_english_passes() -> None:
    draft = b"# Recovery\n\nPut the tablet into Recovery mode.\n"
    assert (
        check_publication_gates(
            draft, source_locale="ru", target_path="ydb/docs/en/core/x.md"
        )
        == ()
    )


def test_en_sentence_case_heading_is_publishable() -> None:
    draft = b"# Cluster maintenance without downtime\n\nBody.\n"
    failures = check_publication_gates(
        draft, source_locale="ru", target_path="ydb/docs/en/core/x.md"
    )
    assert failures == ()


def test_en_title_case_heading_passes() -> None:
    draft = b"# Cluster Maintenance Without Downtime\n\nBody.\n"
    assert (
        check_publication_gates(
            draft, source_locale="ru", target_path="ydb/docs/en/core/x.md"
        )
        == ()
    )


def test_unchanged_sentence_case_heading_vs_previous_is_not_gated() -> None:
    previous = b"# Cluster maintenance without downtime\n\nBody.\n"
    draft = b"# Cluster maintenance without downtime\n\nBody with URL fix.\n"
    assert (
        check_publication_gates(
            draft,
            source_locale="ru",
            target_path="ydb/docs/en/core/x.md",
            previous_target=previous,
        )
        == ()
    )


def test_changed_sentence_case_heading_is_publishable() -> None:
    previous = b"# Maintenance without downtime\n\nBody.\n"
    draft = b"# Cluster maintenance without downtime\n\nBody.\n"
    failures = check_publication_gates(
        draft,
        source_locale="ru",
        target_path="ydb/docs/en/core/x.md",
        previous_target=previous,
    )
    assert failures == ()


def test_split_backtick_spelling_is_left_to_semantic_review() -> None:
    draft = b"See `log`_`config` now.\n"
    failures = check_publication_gates(
        draft, source_locale="ru", target_path="ydb/docs/en/core/x.md"
    )
    assert failures == ()


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


def test_heading_without_blank_line_above_is_publishable() -> None:
    draft = (
        b"**KiKiMR** is the legacy name, and so on.\n"
        b"### Tablet recovery mode {#tablet-recovery-mode}\n\n"
        b"**Recovery mode** is a restore mode.\n"
    )
    failures = check_publication_gates(
        draft, source_locale="ru", target_path="ydb/docs/en/core/concepts/glossary.md"
    )
    assert failures == ()


def test_heading_at_start_of_file_does_not_need_blank_above() -> None:
    draft = b"### Tablet Recovery Mode {#tablet-recovery-mode}\n\nBody.\n"
    assert (
        check_publication_gates(
            draft, source_locale="ru", target_path="ydb/docs/en/core/concepts/glossary.md"
        )
        == ()
    )


def test_unlabeled_opening_fence_is_publishable() -> None:
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
    assert failures == ()


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


def test_fence_layout_is_left_to_document_review() -> None:
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
    assert failures == ()


def test_join_soft_wrapped_prose_merges_adjacent_paragraph_lines() -> None:
    draft = (
        b"**Hash shuffle** join is used to process data.\n"
        b"Such columns are also called key columns.\n"
    )
    assert join_soft_wrapped_prose(draft) == (
        b"**Hash shuffle** join is used to process data. "
        b"Such columns are also called key columns.\n"
    )
    assert (
        check_publication_gates(
            join_soft_wrapped_prose(draft),
            source_locale="ru",
            target_path="ydb/docs/en/core/dev/optimization/structure.md",
        )
        == ()
    )


def test_blank_separated_paragraphs_are_not_joined() -> None:
    draft = (
        b"**Hash shuffle** join is used to process data.\n"
        b"\n"
        b"Such columns are also called key columns.\n"
    )
    assert join_soft_wrapped_prose(draft) == draft


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


def test_cyrillic_identifier_and_code_example_are_publishable():
    draft = '# Example\n\nUser: Иван.\n\n```\nSELECT "Привет";\n```\n'.encode()
    assert check_publication_gates(draft, source_locale="ru",
        target_path="ydb/docs/en/core/example.md") == ()


def test_recorded_benchmarks_translation_is_publishable_without_rewriting():
    from pathlib import Path
    draft = (Path(__file__).parents[2] / "fixtures/pr53925/benchmarks-translated.md").read_bytes()
    assert check_publication_gates(draft, source_locale="ru",
        target_path="ydb/docs/en/core/recipes/ydb-cli/benchmarks.md") == ()


def test_include_examples_in_code_are_not_dependencies():
    draft = (b'```markdown\n{% include [example](missing.md) %}\n```\n\n'
             b'~~~text\n{% include [example](missing2.md) %}\n~~~\n\n'
             b'Use `{% include [example](missing3.md) %}`.\n')
    assert check_publication_gates(draft, source_locale='ru',
        target_path='ydb/docs/en/core/example.md', available_paths=set()) == ()


def test_real_includes_survive_escaped_ticks_paragraphs_and_list_indentation() -> None:
    include = '{% include [actual](missing.md) %}'
    for text in (
        'Use \\` literally.\n\n' + include + '\n\nUse \\` literally.',
        'Unmatched ` tick.\n\n' + include + '\n\nAnother ` tick.',
        '- Item\n\n    ' + include + '\n',
    ):
        assert missing_relative_includes(
            text, target_path='docs/page.md', available_paths=set()
        ) == ('missing.md',)


def test_include_examples_inside_containers_are_not_dependencies() -> None:
    include = '{% include [example](missing.md) %}'
    for text in (
        '> ```markdown\n> ' + include + '\n> ```\n',
        '- Item\n\n    ```markdown\n    ' + include + '\n    ```\n',
        '    ' + include + '\n',
        '````markdown\n```\n' + include + '\n```\n````\n',
    ):
        assert missing_relative_includes(
            text, target_path='docs/page.md', available_paths=set()
        ) == ()


def test_invalid_utf8_still_blocks_publication() -> None:
    failures = check_publication_gates(
        b'\xff', source_locale='ru', target_path='docs/page.md'
    )
    assert [failure.code for failure in failures] == ['utf8_invalid']
