from __future__ import annotations

import json
from itertools import pairwise
from typing import cast

import pytest

from ydbdoc_review_ng.domain import (
    FilePair,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import PairKey
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import Document, InvalidTranslationResponse, RuntimeContent
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError
from ydbdoc_review_ng.scope import FileOperation, ScopeEntry, ScopeOrigin
from ydbdoc_review_ng.translation import (
    DocumentTranslationError,
    assemble_candidate,
    build_document_prompt,
    build_translation_request,
    prepare_document,
)

SOURCE_PATH = RepoPath("ydb/docs/ru/core/page.md")
TARGET_PATH = RepoPath("ydb/docs/en/core/page.md")
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


class ScriptedModels:
    def __init__(self, responses: list[str | ModelCallResult]) -> None:
        self.responses = responses
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        response = self.responses[len(self.calls)]
        self.calls.append(request)
        if type(response) is ModelCallResult:
            return response
        return ModelCallResult(cast(str, response), None, ())


class EchoChunkModels:
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        return ModelCallResult(request.prompt.split("\n\n", 1)[1], None, ())


def _heading_block(number: int, length: int) -> str:
    prefix = f"## Block {number:03d} "
    return prefix + "x" * (length - len(prefix) - 1) + "\n"


def content_filter_witness(*, with_leading_chunk: bool = False) -> bytes:
    lengths = [157] * 49 + [177] + [130] * 60 + [131]
    witness = "".join(
        _heading_block(number, length) for number, length in enumerate(lengths)
    )
    if not with_leading_chunk:
        return witness.encode()
    return (_heading_block(999, 15_900) + witness).encode()


def document_for(source: bytes, *, source_locale: Locale = Locale.RU) -> Document:
    target_locale = Locale.EN if source_locale is Locale.RU else Locale.RU
    source_path, target_path = (
        (SOURCE_PATH, TARGET_PATH) if source_locale is Locale.RU else (TARGET_PATH, SOURCE_PATH)
    )
    key = PairKey(RepoPath("page.md"))
    entry = ScopeEntry(
        FilePair(source_locale, target_locale, source_path, target_path),
        source,
        b"# Old target\n",
        ScopeOrigin.INITIAL,
        FileOperation.TRANSLATE,
        (key,),
        None,
        None,
    )
    plan = build_markdown_plan(SNAPSHOT, source_path, source)
    return Document(entry, source, plan, build_translation_request(source, plan))


def content_with(models: object, environment: dict[str, str] | None = None) -> RuntimeContent:
    return RuntimeContent(
        cast(RuntimeSource, object()),
        cast(RecordedModels, models),
        environment or {},
    )


@pytest.mark.parametrize(
    ("source_locale", "direction"),
    [(Locale.RU, "from ru to en"), (Locale.EN, "from en to ru")],
)
def test_translate_document_uses_complete_markdown_and_selected_direction(
    source_locale: Locale, direction: str
) -> None:
    document = document_for(
        "# Исходный заголовок\n\nТекст с [руководством](guide.md).\n\n- Один\n- Два\n".encode(),
        source_locale=source_locale,
    )
    response = (
        "# Translated heading\n\nText with "
        "[[YDBDOC_PROTECTED_0001]]guide[[YDBDOC_PROTECTED_0002]].\n\n- One\n- Two\n"
    )
    models = ScriptedModels([response])

    accepted = content_with(models).translate_document(document)

    assert len(models.calls) == 1
    call = models.calls[0]
    assert f"Translate the complete Markdown below {direction}" in call.prompt
    assert "# Исходный заголовок" in call.prompt
    assert "- Один\n- Два" in call.prompt
    assert "# Old target" not in call.prompt
    assert "JSON" not in call.prompt
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == b"# Translated heading\n\nText with [guide](guide.md).\n\n- One\n- Two\n"
    )


@pytest.mark.parametrize("source_locale", [Locale.RU, Locale.EN])
def test_large_document_uses_minimum_response_safe_raw_chunks(
    source_locale: Locale,
) -> None:
    source = "\n\n".join(
        f"Paragraph {number:03d} " + "x" * 775 for number in range(140)
    ).encode() + b"\n"
    assert 110_000 < len(source.decode()) < 112_000
    document = document_for(source, source_locale=source_locale)
    models = EchoChunkModels()

    accepted = content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
    ).translate_document(document)

    raw_chunks = tuple(call.prompt.split("\n\n", 1)[1] for call in models.calls)
    assert len(raw_chunks) > 1
    assert all(len(chunk) <= 16_000 for chunk in raw_chunks)
    assert all(
        len(left + right) > 16_000
        for left, right in pairwise(raw_chunks)
    )
    assert all(call.max_tokens == 8_000 and call.schema is None for call in models.calls)
    assert "".join(raw_chunks).encode() == source
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == source
    )


@pytest.mark.parametrize("source_locale", [Locale.RU, Locale.EN])
def test_exhausted_content_filter_splits_only_original_chunk_nearest_midpoint(
    source_locale: Locale,
) -> None:
    source = content_filter_witness(with_leading_chunk=True)
    document = document_for(source, source_locale=source_locale)
    prepared = prepare_document(
        source,
        document.plan,
        max_characters=250_000,
        source_locale=source_locale.value,
        target_locale=(Locale.EN if source_locale is Locale.RU else Locale.RU).value,
    )
    assert [(len(chunk.text), chunk.block_end - chunk.block_start) for chunk in prepared.chunks] == [
        (15_900, 1),
        (15_801, 111),
    ]
    first, filtered = prepared.chunks
    models = ScriptedModels(
        [
            first.text,
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            filtered.text[:7_870],
            filtered.text[7_870:],
        ]
    )

    accepted = content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
    ).translate_document(document)

    raw_requests = tuple(call.prompt.split("\n\n", 1)[1] for call in models.calls)
    assert tuple(map(len, raw_requests)) == (15_900, 15_801, 7_870, 7_931)
    assert raw_requests.count(first.text) == 1
    assert raw_requests[2] + raw_requests[3] == filtered.text
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == source
    )


def test_content_filter_uses_only_boundary_even_when_one_child_exceeds_half_cap() -> None:
    source = (_heading_block(1, 9_000) + _heading_block(2, 1_000)).encode()
    document = document_for(source)
    prepared = prepare_document(
        source,
        document.plan,
        max_characters=250_000,
        source_locale="ru",
        target_locale="en",
    )
    assert [(len(chunk.text), chunk.block_end - chunk.block_start) for chunk in prepared.chunks] == [
        (10_000, 2)
    ]
    models = ScriptedModels(
        [
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            prepared.chunks[0].text[:9_000],
            prepared.chunks[0].text[9_000:],
        ]
    )

    accepted = content_with(models).translate_document(document)

    raw_requests = tuple(call.prompt.split("\n\n", 1)[1] for call in models.calls)
    assert tuple(map(len, raw_requests)) == (10_000, 9_000, 1_000)
    assert raw_requests[1] + raw_requests[2] == raw_requests[0]
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == source
    )


def test_content_filter_in_child_is_terminal_without_recursive_split() -> None:
    source = content_filter_witness()
    document = document_for(source)
    models = ScriptedModels(
        [
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
        ]
    )

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(
            models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
        ).translate_document(document)

    assert len(models.calls) == 2
    assert len(models.calls[0].prompt.split("\n\n", 1)[1]) == 15_801
    assert len(models.calls[1].prompt.split("\n\n", 1)[1]) == 7_870


def test_content_filter_without_top_level_boundary_is_terminal() -> None:
    document = document_for(_heading_block(1, 10_000).encode())
    models = ScriptedModels([ModelCallResult(None, AttemptError.CONTENT_FILTER, ())])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_non_final_parent_does_not_trigger_adaptive_split() -> None:
    document = document_for(content_filter_witness())
    models = ScriptedModels([ModelCallResult(None, AttemptError.NON_FINAL, ())])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_content_filter_on_technical_correction_does_not_split() -> None:
    document = document_for(b"# See [guide](guide.md).\n# Next heading\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    invalid = prepared.chunks[0].text.replace(prepared.placeholders[0].token, "", 1)
    models = ScriptedModels(
        [invalid, ModelCallResult(None, AttemptError.CONTENT_FILTER, ())]
    )

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 2
    assert "Correct the previous invalid translation" in models.calls[1].prompt


def test_complete_markdown_response_gets_exactly_one_technical_correction() -> None:
    document = document_for(b"# See [guide](guide.md).\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    valid = prepared.chunks[0].text
    invalid = valid.replace(prepared.placeholders[0].token, "", 1)
    models = ScriptedModels([invalid, invalid])

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 2
    assert "Correct the previous invalid translation" not in models.calls[0].prompt
    correction = models.calls[1].prompt
    assert "Correct the previous invalid translation" in correction
    assert prepared.chunks[0].text in correction
    assert f"Rejected translation:\n{invalid}" in correction
    assert "Validator error: document_response:placeholder_mismatch" in correction


def test_provider_failure_is_not_semantically_retried() -> None:
    document = document_for(b"# Source heading\n")
    models = ScriptedModels([ModelCallResult(None, AttemptError.TRANSPORT, ())])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_prompt_limit_failure_happens_before_model_call() -> None:
    document = document_for(b"# One complete top-level block that cannot fit.\n")
    models = ScriptedModels([])

    with pytest.raises(DocumentTranslationError, match="top_level_block_exceeds_limit"):
        content_with(
            models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "520"}
        ).translate_document(document)

    assert models.calls == []


def test_response_cap_rejects_one_oversized_top_level_block_before_model_call() -> None:
    document = document_for(("One indivisible paragraph " + "x" * 16_001 + "\n").encode())
    models = ScriptedModels([])

    with pytest.raises(DocumentTranslationError, match="top_level_block_exceeds_limit"):
        content_with(
            models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
        ).translate_document(document)

    assert models.calls == []


def test_near_limit_correction_reservation_fails_before_model_call() -> None:
    document = document_for(b"# See [guide](guide.md).\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    invalid = prepared.chunks[0].text.replace(prepared.placeholders[0].token, "", 1)
    models = ScriptedModels([invalid])
    operator_context = "Reviewer context"
    initial_prompt = (
        build_document_prompt(prepared.chunks[0], "ru", "en")
        + "\n\nOperator context:\n"
        + operator_context
    )
    assert len(initial_prompt) == 589

    with pytest.raises(DocumentTranslationError, match="top_level_block_exceeds_limit"):
        content_with(
            models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "589"}
        ).translate_document(document, operator_context=operator_context)

    assert models.calls == []


def test_multiblock_unit_reserves_exactly_two_calls_within_limit() -> None:
    source = b"\n\n".join(
        (
            b"Paragraph one has thirty seven letters.",
            b"Paragraph two has thirty seven letters.",
            b"Paragraph three has thirty five chars.",
        )
    ) + b"\n"
    document = document_for(source)
    valid = source.decode()
    invalid = valid.replace("\n\n", "\n", 1)
    models = ScriptedModels([invalid, valid])
    operator_context = "Reviewer context"

    content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "900"}
    ).translate_document(document, operator_context=operator_context)

    assert len(models.calls) == 2
    assert all(len(call.prompt) <= 900 for call in models.calls)


def test_nested_yfm_fence_code_mutation_gets_one_technical_correction() -> None:
    source = (
        b'{% note info "Title" %}\n```python\nprint("OPAQUE_CODE")\n'
        b"# Translatable comment\n```\n{% endnote %}\n"
    )
    document = document_for(source)
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    valid = prepared.chunks[0].text
    invalid = valid.replace("OPAQUE_CODE", "MUTATED_CODE").replace(
        "[[YDBDOC_PROTECTED_0001]]", 'print("MUTATED_CODE")'
    )
    models = ScriptedModels([invalid, valid])

    content_with(models).translate_document(document)

    assert len(models.calls) == 2
    assert "OPAQUE_CODE" not in models.calls[0].prompt
    assert "Validator error: document_response:placeholder_mismatch" in models.calls[1].prompt


def test_translation_trace_is_payload_free(capsys: pytest.CaptureFixture[str]) -> None:
    document = document_for(b"# PRIVATE SOURCE HEADING\n")
    models = ScriptedModels(["# PRIVATE TRANSLATED HEADING\n"])

    content_with(models).translate_document(document)

    output = capsys.readouterr().err
    events = [json.loads(line.removeprefix("YDBDOC_TRACE ")) for line in output.splitlines()]
    assert [(event["operation"], event["status"]) for event in events] == [
        ("chunk", "start"),
        ("chunk", "ok"),
    ]
    assert "PRIVATE SOURCE" not in output
    assert "PRIVATE TRANSLATED" not in output
