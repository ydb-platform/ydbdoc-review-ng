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
    assert "Project glossary (SOURCE/TARGET pairs) is reference context only" in prompt


def test_short_bold_emphasis_does_not_match_every_document() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    source = "## Rule {#rule}\n\n**не** является термином.\n".encode()
    target = b"## Rule {#rule}\n\n**not** a term.\n"

    assert bilingual_glossary_context("Unrelated article", source, target) is None


def test_all_relevant_paired_sections_are_included_without_entry_limit() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    terms = (
        "aardvark",
        "badger",
        "cougar",
        "dolphin",
        "echidna",
        "ferret",
        "giraffe",
        "hamster",
        "iguana",
    )
    source_entries = "\n\n".join(
        f"#### Source {term} {{#{term}}}\n\n**{term}** source definition."
        for term in (*terms, "jackal")
    )
    target_entries = "\n\n".join(
        f"#### Target {term} {{#{term}}}\n\n**target {term}** target definition."
        for term in (*terms, "jackal")
    )

    context = bilingual_glossary_context(
        " ".join(terms), source_entries.encode(), target_entries.encode()
    )

    assert context is not None
    assert context.count("<glossary-entry") == 9
    for term in terms:
        assert f'anchor="{term}"' in context
        assert f"**{term}** source definition." in context
        assert f"**target {term}** target definition." in context
    assert 'anchor="jackal"' not in context


def test_selected_paired_section_is_not_truncated_at_eight_thousand_characters() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    source_section = "#### Aardvark {#aardvark}\n\n**aardvark**\n\n" + "source " * 1_399 + "source"
    target_section = (
        "#### Aardvark target {#aardvark}\n\n**target aardvark**\n\n"
        + "target " * 1_399
        + "target"
    )

    context = bilingual_glossary_context(
        "aardvark", source_section.encode(), target_section.encode()
    )

    assert context is not None
    assert len(source_section) > 8_000
    assert len(target_section) > 8_000
    assert source_section in context
    assert target_section in context


def test_target_prose_can_select_a_paired_glossary_section() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    source = "#### Строковая таблица {#row-table}\n\n**строковая таблица**.\n".encode()
    target = b"#### Row-oriented table {#row-table}\n\n**row-oriented table**.\n"

    context = bilingual_glossary_context("A row-oriented table stores rows.", source, target)

    assert context is not None
    assert 'anchor="row-table"' in context


def test_equal_relevance_is_ordered_by_anchor() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    source = (
        b"#### Zebra {#zebra}\n\n**zebra** definition.\n\n"
        b"#### Aardvark {#aardvark}\n\n**aardvark** definition.\n"
    )
    target = (
        b"#### Zebra target {#zebra}\n\n**target zebra** definition.\n\n"
        b"#### Aardvark target {#aardvark}\n\n**target aardvark** definition.\n"
    )

    context = bilingual_glossary_context("zebra aardvark", source, target)

    assert context is not None
    assert context.index('anchor="aardvark"') < context.index('anchor="zebra"')


def test_blobdepot_glossary_section_is_selected_when_present_in_both_locales() -> None:
    """Wiring check for #54797: BlobDepot paired section must reach translator prompts."""
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    source = (
        "#### BlobDepot {#blob-depot}\n\n"
        "**BlobDepot** — системная таблетка распределённого хранилища.\n"
    ).encode()
    target = (
        b"#### BlobDepot {#blob-depot}\n\n"
        b"**BlobDepot** is a system tablet in distributed storage.\n"
    )
    context = bilingual_glossary_context(
        "BlobDepot расширяет функциональность виртуальных групп.",
        source,
        target,
    )

    assert context is not None
    assert 'anchor="blob-depot"' in context
    assert "**BlobDepot** — системная таблетка" in context
    assert "**BlobDepot** is a system tablet" in context


def test_each_document_selects_only_its_own_relevant_sections() -> None:
    from ydbdoc_review_ng.terminology import bilingual_glossary_context

    source = (
        b"#### Aardvark {#aardvark}\n\n**aardvark** first definition.\n\n"
        b"#### Badger {#badger}\n\n**badger** second definition.\n"
    )
    target = (
        b"#### Target aardvark {#aardvark}\n\n**target aardvark** first definition.\n\n"
        b"#### Target badger {#badger}\n\n**target badger** second definition.\n"
    )

    first_context = bilingual_glossary_context("aardvark article", source, target)
    second_context = bilingual_glossary_context("badger article", source, target)

    assert first_context is not None
    assert 'anchor="aardvark"' in first_context
    assert 'anchor="badger"' not in first_context
    assert second_context is not None
    assert 'anchor="badger"' in second_context
    assert 'anchor="aardvark"' not in second_context
