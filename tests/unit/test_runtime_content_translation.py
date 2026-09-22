from __future__ import annotations

import json
from typing import cast

import pytest

from ydbdoc_review_ng.continuation import AcceptedMap
from ydbdoc_review_ng.domain import (
    FilePair,
    GitSha,
    Locale,
    ModelRole,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import PairKey
from ydbdoc_review_ng.models import ModelCallResult, ModelRequest
from ydbdoc_review_ng.models.types import mutable_json
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import Document, InvalidTranslationResponse, RuntimeContent
from ydbdoc_review_ng.scope import FileOperation, ScopeEntry, ScopeOrigin
from ydbdoc_review_ng.translation import assemble_candidate, build_translation_request

SOURCE_PATH = RepoPath("ydb/docs/ru/core/page.md")
TARGET_PATH = RepoPath("ydb/docs/en/core/page.md")
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


class ScriptedModels:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        response = self.responses[len(self.calls)]
        self.calls.append(request)
        return ModelCallResult(response, None, ())


def document_for(source: bytes) -> Document:
    key = PairKey(RepoPath("page.md"))
    entry = ScopeEntry(
        FilePair(Locale.RU, Locale.EN, SOURCE_PATH, TARGET_PATH),
        source,
        b"# Old target\n",
        ScopeOrigin.INITIAL,
        FileOperation.TRANSLATE,
        (key,),
        None,
        None,
    )
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    return Document(entry, source, plan, build_translation_request(source, plan))


def content_with(models: ScriptedModels) -> RuntimeContent:
    return RuntimeContent(
        cast(RuntimeSource, object()),
        cast(RecordedModels, models),
        {},
    )


def test_translate_document_calls_model_once_per_field_then_returns_complete_map() -> None:
    document = document_for(b"# Source heading\n\nSource paragraph.\n")
    assert len(document.request.fields) == 2
    translated = ("Translated heading", "Translated paragraph.")
    models = ScriptedModels(
        [
            json.dumps({field.field_id: value})
            for field, value in zip(document.request.fields, translated, strict=True)
        ]
    )

    accepted = content_with(models).translate_document(document)

    assert len(models.calls) == len(document.request.fields)
    for call, field in zip(models.calls, document.request.fields, strict=True):
        assert (call.role, call.model, call.max_tokens) == (
            ModelRole.TRANSLATE,
            "yandexgpt-5.1/latest",
            8000,
        )
        schema = cast(dict[str, object], mutable_json(call.schema))
        assert schema == {
            "type": "object",
            "properties": {field.field_id: {"type": "string"}},
            "required": [field.field_id],
            "additionalProperties": False,
        }
        prompt_fields = json.loads(call.prompt.split("\nFields: ", 1)[1])
        assert prompt_fields == {field.field_id: field.text}
    assert accepted == AcceptedMap(
        TARGET_PATH,
        tuple(sorted(zip(document.request.requested_ids, translated, strict=True))),
    )
    assert (
        assemble_candidate(
            document.source,
            document.plan,
            document.request,
            accepted.as_dict(),
        )
        == b"# Translated heading\n\nTranslated paragraph.\n"
    )


def test_invalid_field_stops_before_merge_or_another_model_call() -> None:
    document = document_for(b"# See https://safe.example/path\n\nSource paragraph.\n")
    first, second = document.request.fields
    models = ScriptedModels(
        [
            json.dumps({first.field_id: "See [[URL_9999]]"}),
            json.dumps({second.field_id: "Translated paragraph."}),
        ]
    )
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content.translate_document(document)

    assert len(models.calls) == 1
    assert content.accepted_maps == ()


def test_zero_field_document_returns_empty_map_without_model_call() -> None:
    source = b"```sql\nSELECT 1;\n```\n"
    document = document_for(source)
    assert document.request.fields == ()
    models = ScriptedModels([])

    accepted = content_with(models).translate_document(document)

    assert accepted == AcceptedMap(TARGET_PATH, ())
    assert models.calls == []
    assert (
        assemble_candidate(
            document.source,
            document.plan,
            document.request,
            accepted.as_dict(),
        )
        == source
    )
