from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import (
    AttemptError,
    AttemptStatus,
    ExecutionConfig,
    HttpRequest,
    HttpResponse,
    ModelRequest,
    ModelTokenPrice,
    ModelUsage,
    NativeYandexClient,
    PerModelPricing,
    TransportFailure,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.runtime import RecordedModels

SECRET = "api-key-secret-canary"
FOLDER = "folder-canary"
SCHEMA = {
    "type": "object",
    "properties": {"field-1": {"type": "string"}},
    "required": ["field-1"],
    "additionalProperties": False,
}


class FakeTransport:
    def __init__(self, *outcomes: HttpResponse | Exception) -> None:
        self.outcomes = iter(outcomes)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def clock() -> Iterator[datetime]:
    value = datetime(2026, 9, 21, tzinfo=UTC)
    while True:
        yield value
        value += timedelta(milliseconds=1)


def native_response(
    *,
    text: object = '{"field-1":"Hello"}',
    status: str = "ALTERNATIVE_STATUS_FINAL",
    reasoning: object = "0",
) -> HttpResponse:
    return HttpResponse(
        200,
        json.dumps(
            {
                "result": {
                    "alternatives": [
                        {
                            "status": status,
                            "message": {"role": "assistant", "text": text},
                        }
                    ],
                    "usage": {
                        "inputTextTokens": "100",
                        "completionTokens": "20",
                        "totalTokens": "120",
                        "completionTokensDetails": {"reasoningTokens": reasoning},
                    },
                    "modelVersion": "yandexgpt-5.1/latest",
                }
            }
        ).encode(),
    )


def openai_response(
    *,
    text: object = '{"field-1":"Hello"}',
    status: object = "stop",
    reasoning: object = 0,
) -> HttpResponse:
    return HttpResponse(
        200,
        json.dumps(
            {
                "id": "response-1",
                "model": "deepseek-v4-flash/latest",
                "choices": [
                    {
                        "finish_reason": status,
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 10,
                    "total_tokens": 90,
                    "completion_tokens_details": {"reasoning_tokens": reasoning},
                },
            }
        ).encode(),
    )


def request(model: str = "yandexgpt-5.1/latest") -> ModelRequest:
    return ModelRequest(ModelRole.TRANSLATE, model, "translate secret-free prompt", SCHEMA, 321)


def native_client(
    transport: FakeTransport,
    recorded: list[object],
    *,
    pricing: PerModelPricing | None = None,
    execution: ExecutionConfig | None = None,
) -> NativeYandexClient:
    times = clock()
    return NativeYandexClient(
        YandexCredentials(SECRET, FOLDER),
        transport,
        recorded.append,
        pricing=pricing,
        execution=execution or ExecutionConfig(),
        now=lambda: next(times),
    )


def openai_client(
    transport: FakeTransport,
    recorded: list[object],
    *,
    pricing: PerModelPricing | None = None,
) -> YandexOpenAIClient:
    times = clock()
    return YandexOpenAIClient(
        YandexCredentials(SECRET, FOLDER),
        transport,
        recorded.append,
        pricing=pricing,
        now=lambda: next(times),
    )


def test_native_payload_headers_and_short_model_normalization_are_exact() -> None:
    transport = FakeTransport(native_response())
    result = native_client(transport, []).invoke(request())

    sent = transport.requests[0]
    assert sent.url == "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    assert sent.headers == {
        "Authorization": f"Api-Key {SECRET}",
        "Content-Type": "application/json",
        "x-folder-id": FOLDER,
    }
    assert json.loads(sent.body) == {
        "modelUri": f"gpt://{FOLDER}/yandexgpt-5.1/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0,
            "maxTokens": "321",
            "reasoningOptions": {"mode": "DISABLED"},
        },
        "messages": [{"role": "user", "text": "translate secret-free prompt"}],
        "jsonSchema": {"schema": SCHEMA},
    }
    assert result.success


def test_openai_payload_headers_and_full_model_uri_are_exact() -> None:
    transport = FakeTransport(openai_response())
    model = f"gpt://{FOLDER}/deepseek-v4-flash/latest"
    result = openai_client(transport, []).invoke(request(model))

    sent = transport.requests[0]
    assert sent.url == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    assert sent.headers == {
        "Authorization": f"Api-Key {SECRET}",
        "Content-Type": "application/json",
        "OpenAI-Project": FOLDER,
    }
    assert json.loads(sent.body) == {
        "model": model,
        "stream": False,
        "temperature": 0,
        "max_tokens": 321,
        "reasoning_effort": "none",
        "messages": [{"role": "user", "content": "translate secret-free prompt"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "model_response", "strict": True, "schema": SCHEMA},
        },
    }
    assert result.success
    assert result.attempts[0].request_model == model
    assert result.attempts[0].response_model == "deepseek-v4-flash/latest"
    assert result.attempts[0].response_role == "assistant"


def test_native_raw_text_request_omits_json_schema_and_returns_message_text() -> None:
    transport = FakeTransport(native_response(text="# Complete Markdown\n"))
    raw = ModelRequest(
        ModelRole.REPAIR,
        "yandexgpt-5.1/latest",
        "translate complete Markdown",
        None,
        321,
    )

    result = native_client(transport, []).invoke(raw)

    payload = json.loads(transport.requests[0].body)
    assert "jsonSchema" not in payload
    assert payload["messages"] == [{"role": "user", "text": "translate complete Markdown"}]
    assert result.text == "# Complete Markdown\n"


def test_openai_raw_text_request_omits_response_format_and_returns_message_content() -> None:
    transport = FakeTransport(openai_response(text="# Complete Markdown\n"))
    raw = ModelRequest(
        ModelRole.REPAIR,
        "deepseek-v4-flash/latest",
        "translate complete Markdown",
        None,
        321,
    )

    result = openai_client(transport, []).invoke(raw)

    payload = json.loads(transport.requests[0].body)
    assert "response_format" not in payload
    assert payload["messages"] == [
        {"role": "user", "content": "translate complete Markdown"}
    ]
    assert result.text == "# Complete Markdown\n"


def test_yandex_clients_use_validated_default_request_timeout() -> None:
    native_transport = FakeTransport(native_response())
    openai_transport = FakeTransport(openai_response())

    native_client(native_transport, []).invoke(request())
    openai_client(openai_transport, []).invoke(request("deepseek-v4-flash/latest"))

    assert native_transport.requests[0].timeout_seconds == 180.0
    assert openai_transport.requests[0].timeout_seconds == 180.0


def test_yandex_client_preserves_explicit_request_timeout() -> None:
    transport = FakeTransport(native_response())
    client = NativeYandexClient(
        YandexCredentials(SECRET, FOLDER),
        transport,
        lambda _attempt: None,
        timeout_seconds=12.5,
    )

    client.invoke(request())

    assert transport.requests[0].timeout_seconds == 12.5


def test_success_preserves_roles_models_usage_raw_response_and_decimal_cost() -> None:
    response = native_response()
    transport = FakeTransport(response)
    recorded: list[object] = []
    pricing = PerModelPricing(
        {
            "yandexgpt-5.1/latest": ModelTokenPrice(
                Decimal("0.001"), Decimal("0.003"), Decimal("0.010")
            )
        }
    )

    result = native_client(transport, recorded, pricing=pricing).invoke(request())

    attempt = result.attempts[0]
    assert result.text == '{"field-1":"Hello"}'
    assert attempt.status is AttemptStatus.SUCCEEDED
    assert attempt.request_role is ModelRole.TRANSLATE
    assert attempt.request_model == "yandexgpt-5.1/latest"
    assert attempt.request == request()
    assert json.loads(attempt.request_payload) == json.loads(transport.requests[0].body)
    assert SECRET.encode() not in attempt.request_payload
    assert "translate secret-free prompt" not in repr(attempt)
    assert attempt.response_role == "assistant"
    assert attempt.response_model == "yandexgpt-5.1/latest"
    assert attempt.response_status == "ALTERNATIVE_STATUS_FINAL"
    assert attempt.usage.input_tokens == 100
    assert attempt.usage.output_tokens == 20
    assert attempt.usage.total_tokens == 120
    assert attempt.usage.reasoning_tokens == 0
    assert attempt.cost_rub == Decimal("0.160")
    assert attempt.raw_response == response.body
    assert attempt.started_at < attempt.finished_at
    assert recorded == [attempt]


def test_t017_f09_runtime_prices_ordinary_native_usage_without_synthetic_cost() -> None:
    class Persistence:
        def __init__(self) -> None:
            self.attempts = []

        def __call__(self, attempt, *, job_id) -> None:
            assert job_id == "current-job"
            self.attempts.append(attempt)

    persistence = Persistence()
    models = RecordedModels(
        {"YANDEX_API_KEY": SECRET, "YANDEX_FOLDER_ID": FOLDER},
        persistence,  # type: ignore[arg-type]
        FakeTransport(native_response()),
    )
    models.bind_job("current-job")

    result = models.invoke(request())

    assert result.success
    assert result.attempts[0].cost_rub == Decimal("0.1440")
    assert persistence.attempts == [result.attempts[0]]
    assert models.cost == Decimal("0.1440")


def test_native_actual_nested_reasoning_zero_is_extracted() -> None:
    result = native_client(FakeTransport(native_response(reasoning="0")), []).invoke(request())

    assert result.success
    assert result.attempts[0].usage.reasoning_tokens == 0


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (
            HttpResponse(200, b"not-json", billable_cost_rub=Decimal("1.25")),
            AttemptError.MALFORMED_RESPONSE,
        ),
        (native_response(status="ALTERNATIVE_STATUS_TRUNCATED"), AttemptError.NON_FINAL),
        (native_response(text=""), AttemptError.EMPTY_TEXT),
        (openai_response(status="length"), AttemptError.NON_FINAL),
    ],
)
def test_invalid_received_response_is_recorded_once_with_available_cost(
    response: HttpResponse, expected_error: AttemptError
) -> None:
    transport = FakeTransport(response)
    recorded: list[object] = []
    pricing = PerModelPricing(
        {"yandexgpt-5.1/latest": ModelTokenPrice(Decimal("0.001"), Decimal("0.003"), Decimal(0))}
    )
    client = (
        openai_client(transport, recorded, pricing=pricing)
        if b"choices" in response.body
        else native_client(transport, recorded, pricing=pricing)
    )

    result = client.invoke(request())

    assert not result.success
    assert result.failure is expected_error
    assert len(result.attempts) == 1
    assert recorded == [result.attempts[0]]
    assert result.attempts[0].raw_response == response.body
    assert result.attempts[0].cost_rub is not None


def test_transport_failure_without_response_or_billing_has_unknown_cost_and_redacts_secrets() -> (
    None
):
    transport = FakeTransport(
        TransportFailure("provider echoed api-key-secret-canary", retryable=False)
    )
    recorded: list[object] = []
    client = native_client(transport, recorded)

    result = client.invoke(request())

    attempt = result.attempts[0]
    assert attempt.cost_rub is None
    assert attempt.raw_response is None
    rendered = repr(client) + repr(client.credentials) + repr(transport.requests[0])
    rendered += repr(result) + str(result.failure)
    assert SECRET not in rendered
    assert "translate secret-free prompt" not in repr(result)
    assert attempt.error is AttemptError.TRANSPORT


def test_transport_failure_usage_is_costed_without_inventing_a_response() -> None:
    failure = TransportFailure(
        retryable=False,
        usage=ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7),
    )
    pricing = PerModelPricing(
        {"yandexgpt-5.1/latest": ModelTokenPrice(Decimal("0.10"), Decimal("0.25"), Decimal(0))}
    )
    result = native_client(FakeTransport(failure), [], pricing=pricing).invoke(request())
    attempt = result.attempts[0]
    assert attempt.raw_response is None
    assert attempt.usage == ModelUsage(5, 2, 7, None)
    assert attempt.cost_rub == Decimal("1.00")


def test_explicit_provider_zero_cost_is_not_changed_to_unknown() -> None:
    response = HttpResponse(400, b'{"error":"bad"}', billable_cost_rub=Decimal(0))
    result = native_client(FakeTransport(response), []).invoke(request())
    assert result.attempts[0].cost_rub == Decimal(0)


def test_retryable_status_is_recorded_before_successful_second_attempt() -> None:
    first = HttpResponse(503, b'{"billing":{"costRub":"0.05"}}')
    transport = FakeTransport(first, native_response())
    recorded: list[object] = []

    result = native_client(transport, recorded).invoke(request())

    assert result.success
    assert [attempt.attempt_number for attempt in result.attempts] == [1, 2]
    assert [attempt.status for attempt in result.attempts] == [
        AttemptStatus.FAILED,
        AttemptStatus.SUCCEEDED,
    ]
    assert [attempt.cost_rub for attempt in result.attempts] == [Decimal("0.05"), None]
    assert recorded == list(result.attempts)


def test_native_content_filter_is_recorded_then_retried_with_identical_request() -> None:
    filtered = native_response(
        text="provider filtered this completion",
        status="ALTERNATIVE_STATUS_CONTENT_FILTER",
    )
    first = HttpResponse(200, filtered.body, billable_cost_rub=Decimal("1.25"))
    second = HttpResponse(
        200,
        native_response(text='{"field-1":"Recovered"}').body,
        billable_cost_rub=Decimal("0.75"),
    )
    transport = FakeTransport(first, second)
    recorded: list[object] = []

    result = native_client(transport, recorded).invoke(request())

    assert result.text == '{"field-1":"Recovered"}'
    assert result.failure is None
    assert len(transport.requests) == 2
    assert transport.requests[0] == transport.requests[1]
    assert [attempt.error for attempt in result.attempts] == [
        AttemptError.CONTENT_FILTER,
        None,
    ]
    assert [attempt.cost_rub for attempt in result.attempts] == [
        Decimal("1.25"),
        Decimal("0.75"),
    ]
    assert recorded == list(result.attempts)


def test_native_content_filter_twice_fails_after_exactly_two_audited_attempts() -> None:
    filtered = native_response(
        text="provider filtered this completion",
        status="ALTERNATIVE_STATUS_CONTENT_FILTER",
    )
    response = HttpResponse(200, filtered.body, billable_cost_rub=Decimal("0.40"))
    transport = FakeTransport(response, response, native_response())
    recorded: list[object] = []

    result = native_client(transport, recorded).invoke(request())

    assert result.text is None
    assert result.failure is AttemptError.CONTENT_FILTER
    assert len(transport.requests) == 2
    assert [attempt.attempt_number for attempt in result.attempts] == [1, 2]
    assert [attempt.error for attempt in result.attempts] == [
        AttemptError.CONTENT_FILTER,
        AttemptError.CONTENT_FILTER,
    ]
    assert [attempt.cost_rub for attempt in result.attempts] == [
        Decimal("0.40"),
        Decimal("0.40"),
    ]
    assert recorded == list(result.attempts)


def test_native_truncated_final_remains_nonretryable() -> None:
    truncated = native_response(status="ALTERNATIVE_STATUS_TRUNCATED_FINAL")
    transport = FakeTransport(truncated, native_response())

    result = native_client(transport, []).invoke(request())

    assert result.failure is AttemptError.NON_FINAL
    assert len(result.attempts) == 1
    assert len(transport.requests) == 1


def test_openai_content_filter_is_retried_once_then_stop_succeeds() -> None:
    transport = FakeTransport(
        openai_response(text="filtered", status="content_filter"),
        openai_response(text='{"field-1":"Recovered"}', status="stop"),
    )
    recorded: list[object] = []

    result = openai_client(transport, recorded).invoke(
        request("deepseek-v4-flash/latest")
    )

    assert result.text == '{"field-1":"Recovered"}'
    assert result.failure is None
    assert len(transport.requests) == 2
    assert transport.requests[0] == transport.requests[1]
    assert [attempt.response_status for attempt in result.attempts] == [
        "content_filter",
        "stop",
    ]
    assert [attempt.error for attempt in result.attempts] == [
        AttemptError.CONTENT_FILTER,
        None,
    ]
    assert recorded == list(result.attempts)


def test_nonretryable_and_exhausted_failures_have_bounded_attempt_counts() -> None:
    nonretryable_transport = FakeTransport(HttpResponse(400, b"bad"), native_response())
    nonretryable = native_client(nonretryable_transport, []).invoke(request())
    assert len(nonretryable.attempts) == 1
    assert nonretryable.failure is AttemptError.HTTP_STATUS

    exhausted_transport = FakeTransport(HttpResponse(503, b"busy"), HttpResponse(503, b"busy"))
    exhausted = native_client(exhausted_transport, []).invoke(request())
    assert len(exhausted.attempts) == 2
    assert exhausted.failure is AttemptError.HTTP_STATUS


def test_http_error_preserves_nested_provider_status_without_response_text() -> None:
    transport = FakeTransport(HttpResponse(400, b'{"error":{"code":"INVALID_ARGUMENT"}}'))
    result = native_client(transport, []).invoke(request())

    assert result.failure is AttemptError.HTTP_STATUS
    assert result.attempts[0].response_status == "INVALID_ARGUMENT"


def test_recorder_failure_stops_before_retry_and_is_not_hidden() -> None:
    transport = FakeTransport(HttpResponse(503, b"busy"), native_response())

    def broken_recorder(_attempt: object) -> None:
        raise RuntimeError("audit unavailable")

    times = clock()
    client = NativeYandexClient(
        YandexCredentials(SECRET, FOLDER),
        transport,
        broken_recorder,
        now=lambda: next(times),
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        client.invoke(request())
    assert len(transport.requests) == 1


@pytest.mark.parametrize("response", [openai_response(reasoning=2)])
def test_translate_rejects_reported_nonzero_reasoning_without_semantic_retry(
    response: HttpResponse,
) -> None:
    transport = FakeTransport(response, response)
    recorded: list[object] = []
    client = (
        openai_client(transport, recorded)
        if b"choices" in response.body
        else native_client(transport, recorded)
    )
    result = client.invoke(request())
    assert result.failure is AttemptError.REASONING_NOT_DISABLED
    assert len(result.attempts) == 1
    assert result.attempts[0].usage.reasoning_tokens in {1, 2}


def test_native_actual_nested_nonzero_reasoning_rejects_translate_without_retry() -> None:
    response = native_response(reasoning="7")
    transport = FakeTransport(response, response)

    result = native_client(transport, []).invoke(request())

    assert result.failure is AttemptError.REASONING_NOT_DISABLED
    assert len(result.attempts) == 1
    assert result.attempts[0].usage.reasoning_tokens == 7


def test_gpt_oss_translate_is_rejected_without_starting_an_attempt() -> None:
    transport = FakeTransport(openai_response())
    result = openai_client(transport, []).invoke(request("gpt-oss-120b/latest"))
    assert result.failure is AttemptError.UNSUPPORTED_MODEL
    assert result.attempts == ()
    assert transport.requests == []
