from __future__ import annotations


def test_relevant_bilingual_glossary_section_is_added_to_prompt_context() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context
    from ydbdoc_review_ng.translation.document import DocumentChunk, build_document_prompt

    source_glossary = """# Glossary\n\n#### Строковая таблица {#row-oriented-table}\n\n**Строковые таблицы** или **row-oriented tables** хранят строки вместе.\n\n#### Топик {#topic}\n\nОчередь сообщений.\n""".encode()
    target_glossary = b"""# Glossary\n\n#### Row-oriented table {#row-oriented-table}\n\n**Row-oriented tables** store rows together.\n\n#### Topic {#topic}\n\nA message queue.\n"""
    context = bilingual_glossary_context(
        "Для строковых таблиц поддерживается JOIN.", source_glossary, target_glossary
    )

    prompt = build_document_prompt(
        DocumentChunk("Для строковых таблиц поддерживается JOIN.\n", 0, 1, ()),
        "ru",
        "en",
        terminology_context=context,
    )

    assert "Строковые таблицы" in prompt
    assert "Row-oriented tables" in prompt
    assert "Очередь сообщений" not in prompt
    assert "Use the following project glossary" in prompt


def test_short_bold_emphasis_does_not_match_every_document() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    source = "## Rule {#rule}\n\n**не** является термином.\n".encode()
    target = b"## Rule {#rule}\n\n**not** a term.\n"

    assert bilingual_glossary_context("Unrelated article", source, target) is None


def test_glossary_context_is_bounded_and_prefers_more_relevant_entries() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    source_entries = "\n\n".join(
        f"#### Term {index} {{#term-{index}}}\n\n"
        f"**{'priority alpha beta gamma' if index == 19 else 'common term'}**."
        for index in range(20)
    )
    target_entries = "\n\n".join(
        f"#### Term {index} {{#term-{index}}}\n\n"
        f"**{'priority alpha beta gamma' if index == 19 else 'common target'}**."
        for index in range(20)
    )

    context = bilingual_glossary_context(
        "common term and priority alpha beta gamma", source_entries.encode(), target_entries.encode()
    )

    assert context is not None
    assert len(context) <= 24_000
    assert 'anchor="term-19"' in context
    assert context.count("<glossary-entry") <= 12
