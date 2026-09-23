from __future__ import annotations

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.translation.document import (
    DocumentTranslationError,
    build_document_prompt,
    prepare_document,
    restore_document,
)

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/ru/test.md")


def prepared(source: bytes, *, limit: int = 100_000):
    plan = build_markdown_plan(SNAPSHOT, PATH, source)
    return plan, prepare_document(source, plan, max_characters=limit)


@pytest.mark.parametrize(("source_locale", "target_locale"), [("ru", "en"), ("en", "ru")])
def test_complete_markdown_prompt_uses_selected_direction(
    source_locale: str, target_locale: str
) -> None:
    source = "# Заголовок\n\nАбзац с [руководством](guide.md).\n\n- Первый пункт\n- Второй пункт\n".encode()
    _plan, request = prepared(source)

    assert len(request.chunks) == 1
    prompt = build_document_prompt(request.chunks[0], source_locale, target_locale)
    assert f"from {source_locale} to {target_locale}" in prompt
    assert "# Заголовок" in prompt
    assert "Абзац с" in prompt
    assert "- Первый пункт\n- Второй пункт" in prompt
    assert "guide.md" not in prompt
    assert "[[YDBDOC_PROTECTED_0001]]" in prompt
    assert "JSON" not in prompt


def test_global_placeholders_restore_exact_bytes_and_reject_contract_drift() -> None:
    source = b"See `SELECT 1` and [guide](guide.md).\n"
    plan, request = prepared(source)
    text = request.chunks[0].text
    tokens = tuple(item.token for item in request.placeholders)
    assert tokens == tuple(dict.fromkeys(tokens))
    assert "SELECT 1" not in text and "guide.md" not in text
    assert restore_document(source, plan, request, (text,)) == source

    mutations = (
        text.replace(tokens[0], "", 1),
        text.replace(tokens[0], tokens[0] + tokens[0], 1),
        text.replace(tokens[0], "[[YDBDOC_PROTECTED_9999]]", 1),
        text.replace(tokens[0], "TEMP", 1)
        .replace(tokens[1], tokens[0], 1)
        .replace("TEMP", tokens[1], 1),
    )
    for invalid in mutations:
        with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
            restore_document(source, plan, request, (invalid,))


def test_limit_uses_minimum_ordered_whole_block_chunks() -> None:
    source = b"# One\n\nParagraph two.\n\n- Three\n\nFinal four.\n"
    plan = build_markdown_plan(SNAPSHOT, PATH, source)
    block_lengths = [block.span.end - block.span.start for block in plan.blocks]
    limit = sum(block_lengths[:3])

    request = prepare_document(source, plan, max_characters=limit)

    assert len(request.chunks) == 2
    assert "".join(chunk.text for chunk in request.chunks).encode() == source
    assert request.chunks[0].block_end == 3
    assert request.chunks[1].block_start == 3


def test_complete_candidate_reparse_preserves_structural_block_kinds() -> None:
    source = b"# Heading\n\n- First\n- Second\n\n| A | B |\n| - | - |\n| x | y |\n"
    plan, request = prepared(source)
    translated = request.chunks[0].text.replace("Heading", "Title").replace("First", "One")

    candidate = restore_document(source, plan, request, (translated,))

    target_plan = build_markdown_plan(SNAPSHOT, PATH, candidate)
    assert tuple(block.kind for block in target_plan.blocks) == tuple(
        block.kind for block in plan.blocks
    )
