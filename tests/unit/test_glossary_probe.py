from __future__ import annotations

import pytest

from pr_translation_smoke.glossary_probe import (
    build_glossary_prompt,
    mask_opaque_fragments,
    restore_opaque_fragments,
    split_markdown_chunks,
)


def test_probe_leaves_markdown_emphasis_visible_but_restores_opaque_fragments() -> None:
    source = (
        "# Заголовок\n\n"
        "**Термин** и `SELECT * FROM table` [ссылка](https://example.com/a_(b))\n\n"
        "```yaml\nkey: value\n```\n\n"
        "{{ ydb-short-name }}\n"
    )

    masked, fragments = mask_opaque_fragments(source)

    assert "**Термин**" in masked
    assert "`SELECT * FROM table`" not in masked
    assert "https://example.com/a_(b)" not in masked
    assert "```yaml" not in masked
    assert "{{ ydb-short-name }}" not in masked
    assert len(fragments) == 4

    translated = masked.replace("Заголовок", "Title").replace("Термин", "Term")
    assert restore_opaque_fragments(translated, fragments) == (
        "# Title\n\n**Term** и `SELECT * FROM table` [ссылка](https://example.com/a_(b))\n\n"
        "```yaml\nkey: value\n```\n\n"
        "{{ ydb-short-name }}\n"
    )


def test_probe_rejects_lost_or_invented_tokens() -> None:
    masked, fragments = mask_opaque_fragments("Text `code` and [link](https://example.com).")

    with pytest.raises(ValueError, match="opaque token contract"):
        restore_opaque_fragments(masked.replace("[[OPAQUE_0001]]", ""), fragments)

    with pytest.raises(ValueError, match="opaque token contract"):
        restore_opaque_fragments(masked + " [[OPAQUE_9999]]", fragments)


def test_glossary_prompt_requests_complete_markdown_only() -> None:
    prompt = build_glossary_prompt(
        source_path="ydb/docs/ru/core/concepts/glossary.md",
        source_locale="ru",
        target_locale="en",
        masked_markdown="# Заголовок\n\n**Термин** [[OPAQUE_0001]]",
    )

    assert "Return only the complete translated Markdown" in prompt
    assert "Do not summarize" in prompt
    assert "**Термин**" in prompt
    assert "[[OPAQUE_0001]]" in prompt


def test_glossary_chunks_preserve_markdown_without_rewriting_it() -> None:
    source = "# One\n\nparagraph\n# Two\n\nsecond paragraph\n"
    chunks = split_markdown_chunks(source, max_chars=12)

    assert "".join(chunks) == source
    assert all(chunks)


def test_glossary_chunks_do_not_split_fenced_code() -> None:
    source = "before\n```yaml\nkey: value\n```\nafter\n"

    chunks = split_markdown_chunks(source, max_chars=15)

    fenced = next(chunk for chunk in chunks if "```yaml" in chunk)
    assert fenced.count("```") == 2
