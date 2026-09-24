from __future__ import annotations

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind
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
    assert (
        "headings, link labels, image alt text, supported code comments, and translatable "
        "frontmatter values" in prompt
    )


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
    )
    for invalid in mutations:
        with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
            restore_document(source, plan, request, (invalid,))


@pytest.mark.parametrize(
    "invalid",
    [
        "See  and [[YDBDOC_PROTECTED_0002]].\n",
        (
            "See [[YDBDOC_PROTECTED_0001]][[YDBDOC_PROTECTED_0001]] and "
            "[[YDBDOC_PROTECTED_0002]].\n"
        ),
        "See [[YDBDOC_PROTECTED_9999]] and [[YDBDOC_PROTECTED_0002]].\n",
        (
            "See [[YDBDOC_PROTECTED_X]] [[YDBDOC_PROTECTED_0001]] and "
            "[[YDBDOC_PROTECTED_0002]].\n"
        ),
    ],
    ids=["missing", "repeated", "numeric-unknown", "malformed-unknown"],
)
def test_placeholder_namespace_drift_is_rejected(invalid: str) -> None:
    source = b"See `code` and guide.md.\n"
    plan, request = prepared(source)
    assert request.chunks[0].text == (
        "See [[YDBDOC_PROTECTED_0001]] and [[YDBDOC_PROTECTED_0002]].\n"
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (invalid,))


def test_field_local_mobility_rejects_cross_block_inline_code_move() -> None:
    source = b"First `ONE`.\n\nSecond `TWO`.\n"
    plan, request = prepared(source)
    text = request.chunks[0].text
    first, second = (item.token for item in request.placeholders)
    moved = text.replace(first, "TEMP", 1).replace(second, first, 1).replace("TEMP", second, 1)

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (moved,))


def test_identical_inline_code_tokens_cannot_exchange_source_fields() -> None:
    source = b"First `SAME`.\n\nSecond `SAME`.\n"
    plan, request = prepared(source)
    assert request.chunks[0].text == (
        "First [[YDBDOC_PROTECTED_0001]].\n\n"
        "Second [[YDBDOC_PROTECTED_0002]].\n"
    )
    exchanged = (
        "First [[YDBDOC_PROTECTED_0002]].\n\n"
        "Second [[YDBDOC_PROTECTED_0001]].\n"
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (exchanged,))


def test_identical_link_opening_token_ids_cannot_exchange_pairs() -> None:
    source = b"Read [one](one.md), then [two](two.md).\n"
    plan, request = prepared(source)
    assert request.chunks[0].text == (
        "Read [[YDBDOC_PROTECTED_0001]]one[[YDBDOC_PROTECTED_0002]], then "
        "[[YDBDOC_PROTECTED_0003]]two[[YDBDOC_PROTECTED_0004]].\n"
    )
    exchanged = (
        "Read [[YDBDOC_PROTECTED_0003]]one[[YDBDOC_PROTECTED_0002]], then "
        "[[YDBDOC_PROTECTED_0001]]two[[YDBDOC_PROTECTED_0004]].\n"
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (exchanged,))


def test_supported_fence_identical_inline_tokens_cannot_exchange_comment_fields() -> None:
    source = b"```python\n# First `SAME`.\n# Second `SAME`.\n```\n"
    plan, request = prepared(source)
    assert request.chunks[0].text == (
        "```python\n[[YDBDOC_PROTECTED_0001]]First [[YDBDOC_PROTECTED_0002]]."
        "[[YDBDOC_PROTECTED_0003]]Second [[YDBDOC_PROTECTED_0004]]."
        "[[YDBDOC_PROTECTED_0005]]```\n"
    )
    exchanged = request.chunks[0].text.replace(
        "[[YDBDOC_PROTECTED_0002]]", "TEMP", 1
    ).replace("[[YDBDOC_PROTECTED_0004]]", "[[YDBDOC_PROTECTED_0002]]", 1).replace(
        "TEMP", "[[YDBDOC_PROTECTED_0004]]", 1
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (exchanged,))


def test_field_local_mobility_rejects_linked_image_endpoint_repairing() -> None:
    source = b"[![diagram](image.png)](outer.md)\n"
    plan, request = prepared(source)
    text = request.chunks[0].text
    tokens = tuple(item.token for item in request.placeholders)
    assert len(tokens) == 4
    repaired = (
        text.replace(tokens[2], "TEMP", 1)
        .replace(tokens[3], tokens[2], 1)
        .replace("TEMP", tokens[3], 1)
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (repaired,))


def test_field_local_mobility_rejects_opaque_block_reorder() -> None:
    source = b"```text\nOPAQUE_ONE\n```\n\nVisible.\n\n```text\nOPAQUE_TWO\n```\n"
    plan, request = prepared(source)
    text = request.chunks[0].text
    first, second = (item.token for item in request.placeholders)
    reordered = (
        text.replace(first, "TEMP", 1).replace(second, first, 1).replace("TEMP", second, 1)
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (reordered,))


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


def test_missing_final_lf_is_restored_at_each_chunk_boundary() -> None:
    source = b"# First\n# Second\n"
    plan, request = prepared(source, limit=9)
    assert tuple(chunk.text for chunk in request.chunks) == ("# First\n", "# Second\n")

    candidate = restore_document(source, plan, request, ("# First", "# Second"))

    assert candidate == source
    assert build_markdown_plan(SNAPSHOT, PATH, candidate).blocks == plan.blocks


def test_missing_blank_line_at_chunk_boundary_is_cosmetic() -> None:
    source = b"First paragraph.\n\nSecond paragraph.\n"
    plan, request = prepared(source, limit=18)
    assert tuple(chunk.text for chunk in request.chunks) == (
        "First paragraph.\n\n",
        "Second paragraph.\n",
    )

    candidate = restore_document(
        source,
        plan,
        request,
        ("Translated first.", "Translated second."),
    )

    assert candidate == b"Translated first.\nTranslated second.\n"
    assert len(build_markdown_plan(SNAPSHOT, PATH, candidate).blocks) != len(plan.blocks)


def test_missing_blank_line_with_placeholder_is_cosmetic() -> None:
    source = b"First paragraph.\n\nSecond [guide](guide.md).\n"
    plan, request = prepared(source)
    response = request.chunks[0].text.replace("\n\n", "\n", 1)

    candidate = restore_document(source, plan, request, (response,))

    assert candidate == b"First paragraph.\nSecond [guide](guide.md).\n"


def test_whole_document_response_rejects_invented_link() -> None:
    source = b"Plain source.\n"
    plan, request = prepared(source)

    with pytest.raises(DocumentTranslationError, match="structure_mismatch"):
        restore_document(source, plan, request, ("Translated [evil](evil.md).\n",))


def test_whole_document_response_rejects_link_move_between_preserved_blocks() -> None:
    source = b"[Guide](guide.md) first.\n\nSecond paragraph.\n"
    plan, request = prepared(source)
    open_token, close_token = (item.token for item in request.placeholders)
    response = (
        "First paragraph.\n\n"
        f"Second {open_token}Guide{close_token}.\n"
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (response,))


def test_block_merge_does_not_hide_link_move_between_other_blocks() -> None:
    source = b"[Guide](guide.md) first.\n\nSecond paragraph.\n\nThird paragraph.\n"
    plan, request = prepared(source)
    open_token, close_token = (item.token for item in request.placeholders)
    response = (
        "First paragraph.\n\n"
        f"Second {open_token}Guide{close_token}.\n"
        "Third paragraph.\n"
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (response,))


def test_unrelated_block_merge_keeps_inline_code_mobility_in_one_field() -> None:
    source = b"Views `a` and `b`.\n\nSecond paragraph.\n\nThird paragraph.\n"
    plan, request = prepared(source)
    first, second = (item.token for item in request.placeholders)
    response = (
        f"Views {second} and {first}.\n\n"
        "Second paragraph.\nThird paragraph.\n"
    )

    candidate = restore_document(source, plan, request, (response,))

    assert candidate == b"Views `b` and `a`.\n\nSecond paragraph.\nThird paragraph.\n"


def test_block_merge_does_not_hide_inline_code_move_between_fields() -> None:
    source = b"First `a`.\n\nSecond `b`.\n\nThird paragraph.\n"
    plan, request = prepared(source)
    first, second = (item.token for item in request.placeholders)
    response = (
        f"First {second}.\n\n"
        f"Second {first}.\nThird paragraph.\n"
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (response,))


def test_block_merge_does_not_hide_inline_code_swap_between_fields_in_yfm_block() -> None:
    source = (
        b'{% note info "Title `A`" %}\n'
        b"Body `B`\n"
        b"{% endnote %}\n\n"
        b"Second paragraph.\n\nThird paragraph.\n"
    )
    plan, request = prepared(source)
    first, second = (
        item.token
        for item in request.placeholders
        if item.kind is ProtectedKind.INLINE_CODE
    )
    response = request.chunks[0].text
    response = response.replace(first, "TEMP", 1).replace(second, first, 1)
    response = response.replace("TEMP", second, 1).replace(
        "Second paragraph.\n\nThird paragraph.",
        "Second paragraph.\nThird paragraph.",
    )

    with pytest.raises(DocumentTranslationError, match="placeholder_mismatch"):
        restore_document(source, plan, request, (response,))


@pytest.mark.parametrize("invented", ("evil.md", "new_identifier"))
def test_whole_document_response_rejects_invented_path_or_identifier(
    invented: str,
) -> None:
    source = b"Plain source.\n"
    plan, request = prepared(source)

    with pytest.raises(DocumentTranslationError, match="structure_mismatch"):
        restore_document(source, plan, request, (f"Translated {invented}.\n",))


@pytest.mark.parametrize(
    "response",
    (
        "---\ndescription: Translated title\n---\n",
        "Translated title\n",
        "---\ntitle: Translated title\ntitle: Shadow title\n---\n",
    ),
)
def test_whole_document_response_preserves_frontmatter_keys(response: str) -> None:
    source = b"---\ntitle: Source title\n---\n"
    plan, request = prepared(source)

    with pytest.raises(DocumentTranslationError, match="structure_mismatch"):
        restore_document(source, plan, request, (response,))


def test_translated_abbreviation_is_not_treated_as_mutated_source_path() -> None:
    source = (
        "  * `enable_strict_user_management` — включает строгие правила "
        "(т.е. только администратор);\n"
    ).encode()
    plan, request = prepared(source)
    token = request.placeholders[0].token
    response = (
        f"* {token} — enables strict rules (i.e., only an administrator);\n"
    )

    candidate = restore_document(source, plan, request, (response,))

    assert candidate == (
        b"* `enable_strict_user_management` "
        b"\xe2\x80\x94 enables strict rules (i.e., only an administrator);\n"
    )


def test_block_kind_change_is_left_for_critic_review() -> None:
    source = b"# First\n\n# Second\n"
    plan, request = prepared(source)

    candidate = restore_document(source, plan, request, ("# First\n\nSecond",))

    assert candidate == b"# First\n\nSecond\n"


def test_configured_limit_applies_to_each_complete_prompt_with_minimum_chunks() -> None:
    source = b"\n\n".join(
        (
            b"Paragraph one has thirty seven letters.",
            b"Paragraph two has thirty seven letters.",
            b"Paragraph three has thirty five chars.",
            b"Paragraph four has thirty six letters.",
            b"Paragraph five has thirty six letters.",
            b"Paragraph six has thirty seven letters.",
        )
    ) + b"\n"
    plan = build_markdown_plan(SNAPSHOT, PATH, source)
    operator_context = "Reviewer context"

    request = prepare_document(
        source,
        plan,
        max_characters=900,
        source_locale="ru",
        target_locale="en",
        operator_context=operator_context,
    )
    prompts = tuple(
        (
            build_document_prompt(chunk, "ru", "en")
            + "\n\nOperator context:\n"
            + operator_context,
            build_document_prompt(
                chunk,
                "ru",
                "en",
                correction=True,
                rejected_translation=chunk.text,
                validator_error="document_response:placeholder_mismatch",
            )
            + "\n\nOperator context:\n"
            + operator_context,
        )
        for chunk in request.chunks
    )

    assert len(request.chunks) == 2
    assert all(len(prompt) <= 900 for pair in prompts for prompt in pair)
    assert "".join(chunk.text for chunk in request.chunks).encode() == source
    assert all(
        left.block_end == right.block_start
        for left, right in zip(request.chunks, request.chunks[1:], strict=False)
    )


def test_complete_candidate_reparse_preserves_structural_block_kinds() -> None:
    source = b"# Heading\n\n- First\n- Second\n\n| A | B |\n| - | - |\n| x | y |\n"
    plan, request = prepared(source)
    translated = request.chunks[0].text.replace("Heading", "Title").replace("First", "One")

    candidate = restore_document(source, plan, request, (translated,))

    target_plan = build_markdown_plan(SNAPSHOT, PATH, candidate)
    assert tuple(block.kind for block in target_plan.blocks) == tuple(
        block.kind for block in plan.blocks
    )


@pytest.mark.parametrize(
    ("source", "opaque", "visible"),
    [
        (
            b"```text\nOPAQUE_FIELDLESS_FENCE\n```\n\nVisible prose.\n",
            b"OPAQUE_FIELDLESS_FENCE",
            b"Visible prose.",
        ),
        (
            b"```python\nprint('OPAQUE_SUPPORTED_CODE')\n# Translatable comment\n```\n",
            b"OPAQUE_SUPPORTED_CODE",
            b"Translatable comment",
        ),
        (
            (
                b"---\ntitle: Visible title\ndescription: Visible description\n"
                b"layout: OPAQUE_LAYOUT\ntags: [OPAQUE_TAG]\n---\nVisible body.\n"
            ),
            b"OPAQUE_LAYOUT",
            b"Visible title",
        ),
        (
            b"{% include [OPAQUE_INCLUDE](path/to/file.md) %}\n\nVisible prose.\n",
            b"OPAQUE_INCLUDE",
            b"Visible prose.",
        ),
        (
            (
                b'{% note info "Title" %}\n```python\nprint("OPAQUE_NESTED_CODE")\n'
                b"# Translatable comment\n```\n{% endnote %}\n"
            ),
            b"OPAQUE_NESTED_CODE",
            b"Translatable comment",
        ),
        (
            (
                b'{% note info "Visible title" %}\n```text\nOPAQUE_NESTED_TEXT\n```\n'
                b"{% endnote %}\n"
            ),
            b"OPAQUE_NESTED_TEXT",
            b"Visible title",
        ),
    ],
    ids=[
        "fieldless-fence",
        "supported-fence-code",
        "frontmatter",
        "yfm-include",
        "yfm-supported-fence",
        "yfm-unsupported-fence",
    ],
)
def test_source_owned_opaque_bytes_never_reach_model_and_restore_exactly(
    source: bytes, opaque: bytes, visible: bytes
) -> None:
    plan, request = prepared(source)
    model_text = "".join(chunk.text for chunk in request.chunks).encode()

    assert opaque not in model_text
    assert visible in model_text
    assert restore_document(
        source, plan, request, tuple(chunk.text for chunk in request.chunks)
    ) == source
