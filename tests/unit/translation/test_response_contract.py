from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.translation import (
    ResponseError,
    ResponseErrorReason,
    TranslationRequest,
    build_translation_request,
    parse_translation_response,
)

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/en/test.md")


def request_for(source: bytes) -> TranslationRequest:
    plan = build_markdown_plan(SNAPSHOT, PATH, source)
    return build_translation_request(source, plan)


def test_request_exposes_exact_ids_and_placeholder_text_without_target() -> None:
    request = request_for(b"# Read [the guide](/docs/path) and `SELECT 1`\n")
    assert request.requested_ids == tuple(field.field_id for field in request.fields)
    assert len(request.fields) == 1
    assert request.fields[0].text.startswith("Read ")
    assert "/docs/path" not in request.fields[0].text
    assert "SELECT 1" not in request.fields[0].text
    assert request.fields[0].text.count("[[") == len(request.fields[0].placeholders)


def test_request_tokens_do_not_collide_with_literal_prose() -> None:
    request = request_for(b"Literal [[IDENTIFIER_0001]]\n")
    assert request.fields[0].placeholders[0].token == "[[IDENTIFIER_0002]]"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", ResponseErrorReason.MALFORMED_JSON),
        ("{", ResponseErrorReason.MALFORMED_JSON),
        ("[]", ResponseErrorReason.ROOT_NOT_OBJECT),
        ('{"other":"x"}', ResponseErrorReason.FIELD_IDS_MISMATCH),
        ('{"extra":"x","other":"y"}', ResponseErrorReason.FIELD_IDS_MISMATCH),
    ],
)
def test_response_rejects_invalid_shape_without_echoing_payload(
    raw: str, reason: ResponseErrorReason
) -> None:
    request = request_for(b"Hello\n")
    with pytest.raises(ResponseError) as caught:
        parse_translation_response(raw, request)
    assert caught.value.reason is reason
    if raw:
        assert raw not in str(caught.value)


def test_response_rejects_duplicate_requested_id() -> None:
    request = request_for(b"Hello\n")
    field_id = request.requested_ids[0]
    raw = f'{{"{field_id}":"one","{field_id}":"two"}}'
    with pytest.raises(ResponseError) as caught:
        parse_translation_response(raw, request)
    assert caught.value.reason is ResponseErrorReason.DUPLICATE_FIELD_ID


@pytest.mark.parametrize("value", [None, True, 1, [], {}])
def test_response_rejects_every_non_string_json_value(value: object) -> None:
    request = request_for(b"Hello\n")
    raw = json.dumps({request.requested_ids[0]: value})
    with pytest.raises(ResponseError) as caught:
        parse_translation_response(raw, request)
    assert caught.value.reason is ResponseErrorReason.NON_STRING_VALUE


def test_response_accepts_exact_string_map() -> None:
    request = request_for(b"Hello\n")
    expected = {request.requested_ids[0]: "Bonjour"}
    assert parse_translation_response(json.dumps(expected), request) == expected
