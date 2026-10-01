"""Unit tests for TOC visible-string JSON ID-map translation (§3)."""

from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng.domain import ModelRole, RepoPath
from ydbdoc_review_ng.toc_delta import (
    TocStringChange,
    TocStringTranslationError,
    apply_toc_delta,
    build_toc_string_request,
    parse_toc_string_response,
)


TOC = RepoPath("ydb/docs/ru/core/manual/toc_i.yaml")
TARGET = RepoPath("ydb/docs/en/core/manual/toc_i.yaml")


def _changes() -> tuple[TocStringChange, ...]:
    return (
        TocStringChange("items/0/name", "Страница", "name"),
        TocStringChange("items/1/title", "Раздел", "title"),
    )


def test_toc_string_request_is_translate_role_with_closed_json_schema() -> None:
    request = build_toc_string_request(
        _changes(),
        source_locale="ru",
        target_locale="en",
        model="deepseek-v4-flash",
        target_path=TARGET,
    )
    assert request.role is ModelRole.TRANSLATE
    assert request.target_path == TARGET
    assert request.model == "deepseek-v4-flash"
    assert "items/0/name" in request.prompt
    assert "Страница" in request.prompt
    assert request.schema is not None
    assert request.schema["type"] == "object"
    assert set(request.schema["required"]) == {"strings"}


def test_toc_string_response_requires_exact_id_set() -> None:
    changes = _changes()
    raw = json.dumps(
        {"strings": {"items/0/name": "Page", "items/1/title": "Section"}},
        ensure_ascii=False,
    )
    assert parse_toc_string_response(raw, changes) == {
        "items/0/name": "Page",
        "items/1/title": "Section",
    }
    with pytest.raises(TocStringTranslationError):
        parse_toc_string_response(json.dumps({"strings": {"items/0/name": "Page"}}), changes)
    with pytest.raises(TocStringTranslationError):
        parse_toc_string_response("{bad", changes)
    with pytest.raises(TocStringTranslationError):
        parse_toc_string_response(
            json.dumps({"strings": {"items/0/name": "Page", "items/1/title": ""}}),
            changes,
        )


def test_translate_toc_strings_retries_once_then_raises() -> None:
    from typing import cast

    from ydbdoc_review_ng.models import AttemptError, ModelCallResult
    from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
    from ydbdoc_review_ng.runtime_content import RuntimeContent

    class Scripted:
        def __init__(self, responses: list[str | ModelCallResult]) -> None:
            self.responses = responses
            self.calls: list[object] = []

        def invoke(self, request: object, /) -> ModelCallResult:
            self.calls.append(request)
            response = self.responses[len(self.calls) - 1]
            if type(response) is ModelCallResult:
                return response
            return ModelCallResult(cast(str, response), None, ())

    changes = _changes()
    ok = json.dumps(
        {"strings": {"items/0/name": "Page", "items/1/title": "Section"}},
        ensure_ascii=False,
    )
    models = Scripted([ModelCallResult(None, AttemptError.TRANSPORT, ()), ok])
    content = RuntimeContent(
        cast(RuntimeSource, object()), cast(RecordedModels, models), {}
    )
    assert content._translate_toc_strings(
        changes, TARGET, source_locale="ru", target_locale="en"
    ) == {"items/0/name": "Page", "items/1/title": "Section"}
    assert len(models.calls) == 2

    failing = Scripted(
        [
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
        ]
    )
    content = RuntimeContent(
        cast(RuntimeSource, object()), cast(RecordedModels, failing), {}
    )
    with pytest.raises(TocStringTranslationError):
        content._translate_toc_strings(
            changes, TARGET, source_locale="ru", target_locale="en"
        )
    assert len(failing.calls) == 2

    malformed = Scripted(["{bad", "{still-bad"])
    content = RuntimeContent(
        cast(RuntimeSource, object()), cast(RecordedModels, malformed), {}
    )
    with pytest.raises(TocStringTranslationError):
        content._translate_toc_strings(
            changes, TARGET, source_locale="ru", target_locale="en"
        )
    assert len(malformed.calls) == 2


def test_apply_toc_delta_inserts_translated_visible_strings() -> None:
    before = "items:\n- name: Старое\n  href: page.md\n".encode()
    after = "items:\n- name: Новое\n  href: page.md\n".encode()
    target = b"items:\n- name: Old EN\n  href: page.md\n"
    draft = apply_toc_delta(before, after, target, toc_path=TOC)
    assert draft.string_changes[0].text == "Новое"
    translated = apply_toc_delta(
        before,
        after,
        target,
        toc_path=TOC,
        translations={draft.string_changes[0].string_id: "New EN"},
    )
    assert translated.content is not None
    assert b"New EN" in translated.content
    assert "Новое".encode() not in translated.content
