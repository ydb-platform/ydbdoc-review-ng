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
from ydbdoc_review_ng.models.context import calculate_context_budget
from ydbdoc_review_ng.quality.critic import build_pr_arbiter_request, build_pr_critic_request


class RecordingTransport:
    def __init__(self, response_text: str = "{}") -> None:
        self.requests: list[HttpRequest] = []
        self.response_text = response_text

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return HttpResponse(
            200,
            json.dumps({"choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": self.response_text},
            }]}).encode(),
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


def test_packing_budget_exposes_only_complete_wire_input_requirement() -> None:
    transport = RecordingTransport()
    model = client(transport)
    request = ModelRequest(ModelRole.CRITIC, "deepseek-v4-flash", "Review full files", None)
    budget = model.prepare_request(request)
    assert budget.input_tokens == len(budget.body)
    assert budget.max_tokens == 1_048_576 - budget.input_tokens
    assert not hasattr(budget, "expected_output_tokens")
    model.invoke(request)
    assert transport.requests[0].body == budget.body


def test_context_helper_requires_only_the_wire_serializer() -> None:
    budget = calculate_context_budget(
        lambda max_tokens: json.dumps({"max_tokens": max_tokens}).encode()
    )
    assert json.loads(budget.body)["max_tokens"] == 1_048_576 - len(budget.body)


def test_model_request_rejects_removed_expected_response_argument() -> None:
    with pytest.raises(TypeError, match="expected_response"):
        ModelRequest(
            ModelRole.CRITIC, "deepseek-v4-flash", "Review", None,
            expected_response={"files": {}},
        )


def test_critic_calls_model_when_input_fits_without_reserving_a_synthetic_reply() -> None:
    transport = RecordingTransport('{"files":{"en.md":"Corrected"}}')
    model = client(transport)
    request = build_pr_critic_request(
        model="deepseek-v4-flash",
        source_files={"ru.md": b"Source"},
        translated_files={"en.md": b"x" * 600_000},
        glossary_files={},
    )
    result = model.invoke(request)
    body = transport.requests[0].body
    assert result.success
    assert json.loads(body)["max_tokens"] == 1_048_576 - len(body)
    assert 0 < json.loads(body)["max_tokens"] < 600_000


def test_response_content_and_size_do_not_change_wire_budget() -> None:
    request = ModelRequest(ModelRole.CRITIC, "deepseek-v4-flash", "Review full files", None)
    short = RecordingTransport('{"files":{"en.md":"Brief"}}')
    long = RecordingTransport(json.dumps({"files": {"en.md": "я🙂\n\"\\" * 4000}}))
    assert len(long.response_text.encode()) > 8000
    for transport in (short, long):
        result = client(transport).invoke(request)
        assert result.success
        assert result.text == transport.response_text
    assert short.requests[0].body == long.requests[0].body
    body = long.requests[0].body
    assert json.loads(body)["max_tokens"] == 1_048_576 - len(body)
    assert json.loads(body)["max_tokens"] > len(long.response_text.encode())


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


def test_impossible_input_is_rejected_before_transport() -> None:
    transport = RecordingTransport()
    model = client(transport)
    request = ModelRequest(
        ModelRole.TRANSLATE,
        "deepseek-v4-flash",
        "x" * 1_048_576,
        None,
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


def test_one_remaining_token_is_available_without_any_response_preflight() -> None:
    transport = RecordingTransport()
    model = client(transport)
    request = ModelRequest(ModelRole.CRITIC, "deepseek-v4-flash", "Review", None)
    baseline = model.prepare_request(request)
    # Replacing a seven-digit max_tokens with one digit releases six bytes.
    prompt_size = len(request.prompt) + baseline.max_tokens + 6 - 1
    boundary = replace(request, prompt="x" * prompt_size)
    budget = model.prepare_request(boundary)
    assert budget.max_tokens == 1
    assert budget.input_tokens == 1_048_575
    model.invoke(boundary)
    assert transport.requests[0].body == budget.body
