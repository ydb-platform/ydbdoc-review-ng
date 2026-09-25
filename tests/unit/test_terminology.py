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
