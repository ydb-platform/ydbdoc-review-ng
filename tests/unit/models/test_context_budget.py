from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import (
    HttpRequest,
    HttpResponse,
    ModelRequest,
    NativeYandexClient,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.quality.critic import build_pr_arbiter_request, build_pr_critic_request


class RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return HttpResponse(
            200,
            b'{"choices":[{"finish_reason":"stop",'
            b'"message":{"role":"assistant","content":"{}"}}]}',
        )


def client(
    transport: RecordingTransport, *, native: bool = False
) -> NativeYandexClient | YandexOpenAIClient:
    cls = NativeYandexClient if native else YandexOpenAIClient
    return cls(YandexCredentials("secret", "folder"), transport, lambda attempt: None)


def test_complete_wire_bytes_determine_output_for_different_prompt_sizes() -> None:
    transport = RecordingTransport()
    model = client(transport)
    for prompt in ("Translate", "Translate " + "я🙂\n\"" * 2000):
        model.invoke(ModelRequest(ModelRole.TRANSLATE, "deepseek-v4-flash", prompt, None))

    bodies = [item.body for item in transport.requests]
    limits = [json.loads(body)["max_tokens"] for body in bodies]
    assert limits[0] != limits[1]
    assert limits == [1_048_576 - len(body) for body in bodies]


@pytest.mark.parametrize("builder", [build_pr_critic_request, build_pr_arbiter_request])
def test_production_review_roles_use_complete_wire_budget(builder) -> None:
    transport = RecordingTransport()
    model = client(transport)
    request = builder(
        model="deepseek-v4-flash",
        source_files={"ru.md": b"Source"},
        translated_files={"en.md": b"Translation" * 2000},
        glossary_files={"glossary.md": "Термин\n".encode() * 300},
        operator_context="Уточните термин",
    )
    model.invoke(request)
    body = transport.requests[0].body
    payload = json.loads(body)
    assert payload["max_tokens"] == 1_048_576 - len(body)
    assert payload["max_tokens"] > 8000
    assert "Термин" in payload["messages"][0]["content"]
    assert "Уточните термин" in payload["messages"][0]["content"]
    assert "response_format" in payload
    if request.role is ModelRole.CRITIC:
        budget = model.prepare_request(request)
        assert budget.expected_output_tokens == len(
            b'{"files":{"en.md":"' + b"Translation" * 2000 + b'"}}'
        )


def test_packing_budget_exposes_full_input_and_expected_json_response_separately() -> None:
    transport = RecordingTransport()
    model = client(transport)
    expected = {"files": {"en.md": "я🙂\n\"\\" * 2000}}
    request = ModelRequest(
        ModelRole.CRITIC, "deepseek-v4-flash", "Review full files", None,
        expected_response=expected,
    )
    budget = model.prepare_request(request)
    expected_bytes = json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    assert budget.expected_output_tokens == len(expected_bytes)
    assert budget.expected_output_tokens > 8000
    assert budget.input_tokens == len(budget.body)
    assert budget.max_tokens == 1_048_576 - budget.input_tokens
    assert budget.expected_output_tokens < budget.max_tokens
    model.invoke(request)
    assert transport.requests[0].body == budget.body


@pytest.mark.parametrize("native", [False, True])
def test_schema_and_normalized_model_uri_consume_wire_budget(native: bool) -> None:
    transport = RecordingTransport()
    model = client(transport, native=native)
    request = ModelRequest(ModelRole.DIRECTION, "deepseek-v4-flash", "Classify", None)
    schema = {"type": "object", "description": "Описание 🙂" * 1000}
    model.invoke(request)
    model.invoke(replace(request, schema=schema))
    first, second = (json.loads(item.body) for item in transport.requests)
    limits = [
        value["completionOptions"]["maxTokens"] if native else value["max_tokens"]
        for value in (first, second)
    ]
    assert int(limits[0]) > int(limits[1])
    for item, limit in zip(transport.requests, limits, strict=True):
        assert int(limit) == 1_048_576 - len(item.body)
        assert b"gpt://folder/deepseek-v4-flash" in item.body


@pytest.mark.parametrize("overflow", ["input", "response"])
def test_impossible_request_is_rejected_before_transport(overflow: str) -> None:
    transport = RecordingTransport()
    model = client(transport)
    request = ModelRequest(
        ModelRole.TRANSLATE,
        "deepseek-v4-flash",
        "x" * (1_048_576 if overflow == "input" else 1),
        None,
        expected_response={"segment_0001": "x" * (1_048_576 if overflow == "response" else 1)},
    )
    with pytest.raises(ValueError, match="context"):
        model.invoke(request)
    assert transport.requests == []


def test_decimal_digit_boundary_has_an_exact_deterministic_wire_budget() -> None:
    transport = RecordingTransport()
    model = client(transport)
    request = ModelRequest(ModelRole.ARBITER, "deepseek-v4-flash", "x", None)
    baseline = model.prepare_request(request)
    # A compact JSON integer changing from 100000 to 99999 loses one byte;
    # the final serialized representation must still satisfy exact accounting.
    prompt_size = 1_048_576 - 100_000 - (len(baseline.body) - 3)
    boundary = replace(request, prompt="x" * prompt_size)
    budget = model.prepare_request(boundary)
    assert budget.max_tokens == 1_048_576 - len(budget.body)
    assert json.loads(budget.body)["max_tokens"] == budget.max_tokens
    assert budget.max_tokens == 99_999
    assert budget.body.endswith(b" ")
    assert model.prepare_request(boundary) == budget


def test_complete_json_output_fits_exactly_but_one_more_byte_is_rejected() -> None:
    transport = RecordingTransport()
    model = client(transport)
    request = ModelRequest(ModelRole.CRITIC, "deepseek-v4-flash", "Review", None)
    remaining = model.prepare_request(request).max_tokens
    # Count both JSON quotes in addition to the expected response text.
    exact = replace(request, expected_response="x" * (remaining - 2))
    budget = model.prepare_request(exact)
    assert budget.expected_output_tokens == budget.max_tokens
    with pytest.raises(ValueError, match="context"):
        model.invoke(replace(request, expected_response="x" * (remaining - 1)))
    assert transport.requests == []
