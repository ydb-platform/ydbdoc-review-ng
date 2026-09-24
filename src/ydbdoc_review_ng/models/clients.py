"""Yandex model clients with bounded, synchronously audited execution."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models.types import (
    AttemptError,
    AttemptRecorder,
    AttemptResult,
    AttemptStatus,
    CostCalculator,
    ExecutionConfig,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    ModelCallResult,
    ModelRequest,
    ModelUsage,
    TransportFailure,
    YandexCredentials,
    mutable_json,
)

NATIVE_ENDPOINT = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
OPENAI_ENDPOINT = "https://ai.api.cloud.yandex.net/v1/chat/completions"


class _ReadableResponse(Protocol):
    def read(self) -> bytes: ...


def _read_response(response: _ReadableResponse, /) -> bytes:
    try:
        return response.read()
    except http.client.IncompleteRead as error:
        raise TransportFailure(
            error,
            retryable=False,
            raw_response=bytes(error.partial),
        ) from None


class UrllibTransport:
    """Small standard-library transport; tests inject a callable instead."""

    def __call__(self, request: HttpRequest, /) -> HttpResponse:
        wire_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(wire_request, timeout=request.timeout_seconds) as response:
                return HttpResponse(response.status, _read_response(response))
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, _read_response(error))
        except (OSError, urllib.error.URLError) as error:
            raise TransportFailure(error, retryable=True) from None


class _ParsedResponse:
    __slots__ = ("error", "model", "role", "status", "text", "usage")

    def __init__(
        self,
        *,
        error: AttemptError | None,
        model: str | None,
        role: str | None,
        status: str | None,
        text: str | None,
        usage: ModelUsage,
    ) -> None:
        self.error = error
        self.model = model
        self.role = role
        self.status = status
        self.text = text
        self.usage = usage


def _token(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if type(value) is str and value.isdecimal():
        return int(value)
    return None


def _mapping(value: object) -> Mapping[str, object] | None:
    if type(value) is dict:
        return cast(Mapping[str, object], value)
    return None


def _string(value: object) -> str | None:
    return value if type(value) is str else None


def _usage_native(document: Mapping[str, object]) -> ModelUsage:
    result = _mapping(document.get("result"))
    usage = _mapping(result.get("usage")) if result is not None else None
    if usage is None:
        return ModelUsage()
    details = _mapping(usage.get("completionTokensDetails"))
    return ModelUsage(
        _token(usage.get("inputTextTokens")),
        _token(usage.get("completionTokens")),
        _token(usage.get("totalTokens")),
        _token(details.get("reasoningTokens")) if details is not None else None,
    )


def _usage_openai(document: Mapping[str, object]) -> ModelUsage:
    usage = _mapping(document.get("usage"))
    if usage is None:
        return ModelUsage()
    details = _mapping(usage.get("completion_tokens_details"))
    return ModelUsage(
        _token(usage.get("prompt_tokens")),
        _token(usage.get("completion_tokens")),
        _token(usage.get("total_tokens")),
        _token(details.get("reasoning_tokens")) if details is not None else None,
    )


def _parse_native(document: Mapping[str, object], role: ModelRole) -> _ParsedResponse:
    usage = _usage_native(document)
    result = _mapping(document.get("result"))
    alternatives = result.get("alternatives") if result is not None else None
    if type(alternatives) is not list or not alternatives:
        return _ParsedResponse(
            error=AttemptError.MALFORMED_RESPONSE,
            model=None,
            role=None,
            status=None,
            text=None,
            usage=usage,
        )
    alternative = _mapping(alternatives[0])
    message = _mapping(alternative.get("message")) if alternative is not None else None
    status = _string(alternative.get("status")) if alternative is not None else None
    text = _string(message.get("text")) if message is not None else None
    response_role = _string(message.get("role")) if message is not None else None
    model = _string(result.get("modelVersion")) if result is not None else None
    error = (
        AttemptError.CONTENT_FILTER
        if status == "ALTERNATIVE_STATUS_CONTENT_FILTER"
        else _semantic_error(status, "ALTERNATIVE_STATUS_FINAL", text, usage, role)
    )
    return _ParsedResponse(
        error=error,
        model=model,
        role=response_role,
        status=status,
        text=text,
        usage=usage,
    )


def _parse_openai(document: Mapping[str, object], role: ModelRole) -> _ParsedResponse:
    usage = _usage_openai(document)
    choices = document.get("choices")
    if type(choices) is not list or not choices:
        return _ParsedResponse(
            error=AttemptError.MALFORMED_RESPONSE,
            model=_string(document.get("model")),
            role=None,
            status=None,
            text=None,
            usage=usage,
        )
    choice = _mapping(choices[0])
    message = _mapping(choice.get("message")) if choice is not None else None
    status = _string(choice.get("finish_reason")) if choice is not None else None
    text = _string(message.get("content")) if message is not None else None
    response_role = _string(message.get("role")) if message is not None else None
    error = (
        AttemptError.CONTENT_FILTER
        if status == "content_filter"
        else _semantic_error(status, "stop", text, usage, role)
    )
    return _ParsedResponse(
        error=error,
        model=_string(document.get("model")),
        role=response_role,
        status=status,
        text=text,
        usage=usage,
    )


def _semantic_error(
    status: str | None,
    final_status: str,
    text: str | None,
    usage: ModelUsage,
    role: ModelRole,
) -> AttemptError | None:
    if status is None:
        return AttemptError.MALFORMED_RESPONSE
    if status != final_status:
        return AttemptError.NON_FINAL
    if text is None:
        return AttemptError.MALFORMED_RESPONSE
    if not text:
        return AttemptError.EMPTY_TEXT
    if role is ModelRole.TRANSLATE and usage.reasoning_tokens not in {None, 0}:
        return AttemptError.REASONING_NOT_DISABLED
    return None


def _document(body: bytes) -> Mapping[str, object] | None:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _mapping(decoded)


def _provider_cost(document: Mapping[str, object] | None) -> Decimal | None:
    if document is None:
        return None
    billing = _mapping(document.get("billing"))
    if billing is None:
        return None
    value = billing.get("costRub")
    if type(value) not in {str, int, float} or type(value) is bool:
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost >= 0 else None


class _BaseYandexClient:
    endpoint: str

    def __init__(
        self,
        credentials: YandexCredentials,
        transport: HttpTransport,
        recorder: AttemptRecorder,
        *,
        pricing: CostCalculator | None = None,
        execution: ExecutionConfig | None = None,
        now: Callable[[], datetime] | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.credentials = credentials
        self._transport = transport
        self._recorder = recorder
        self._pricing = pricing
        self._execution = execution or ExecutionConfig()
        self._now = now or (lambda: datetime.now(UTC))
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"{type(self).__name__}(credentials=<redacted>)"

    def _payload(self, request: ModelRequest, model_uri: str) -> dict[str, object]:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _parse(self, document: Mapping[str, object], role: ModelRole) -> _ParsedResponse:
        raise NotImplementedError

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        if request.role is ModelRole.TRANSLATE and "gpt-oss" in request.model.lower():
            return ModelCallResult(None, AttemptError.UNSUPPORTED_MODEL, ())
        model_uri = normalize_model_uri(request.model, self.credentials.folder_id)
        body = json.dumps(
            self._payload(request, model_uri), ensure_ascii=False, separators=(",", ":")
        ).encode()
        wire_request = HttpRequest(
            self.endpoint,
            self._headers(),
            body,
            self._timeout_seconds,
        )
        attempts: list[AttemptResult] = []
        for attempt_number in range(1, self._execution.max_attempts + 1):
            started_at = self._now()
            try:
                response = self._transport(wire_request)
            except TransportFailure as failure:
                cost = failure.billable_cost_rub
                if cost is None and self._pricing is not None:
                    cost = self._pricing(request.model, failure.usage)
                attempt = AttemptResult(
                    attempt_number,
                    request.role,
                    request.model,
                    request,
                    body,
                    started_at,
                    self._now(),
                    AttemptStatus.FAILED,
                    AttemptError.TRANSPORT,
                    None,
                    failure.raw_response,
                    None,
                    None,
                    None,
                    None,
                    failure.usage,
                    cost,
                )
                self._recorder(attempt)
                attempts.append(attempt)
                if failure.retryable and attempt_number < self._execution.max_attempts:
                    continue
                return ModelCallResult(None, AttemptError.TRANSPORT, tuple(attempts))
            document = _document(response.body)
            parsed = (
                self._parse(document, request.role)
                if document is not None
                else _ParsedResponse(
                    error=AttemptError.MALFORMED_RESPONSE,
                    model=None,
                    role=None,
                    status=None,
                    text=None,
                    usage=ModelUsage(),
                )
            )
            cost = response.billable_cost_rub
            if cost is None:
                cost = _provider_cost(document)
            if cost is None and self._pricing is not None:
                cost = self._pricing(request.model, parsed.usage)
            if 200 <= response.status_code < 300:
                error = parsed.error
            else:
                error = AttemptError.HTTP_STATUS
            status = AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED
            attempt = AttemptResult(
                attempt_number,
                request.role,
                request.model,
                request,
                body,
                started_at,
                self._now(),
                status,
                error,
                response.status_code,
                response.body,
                parsed.status,
                parsed.model,
                parsed.role,
                parsed.text,
                parsed.usage,
                cost,
            )
            self._recorder(attempt)
            attempts.append(attempt)
            if error is None:
                return ModelCallResult(parsed.text, None, tuple(attempts))
            retryable = (
                error is AttemptError.CONTENT_FILTER
                or response.status_code in self._execution.retryable_statuses
            )
            if retryable and attempt_number < self._execution.max_attempts:
                continue
            return ModelCallResult(None, error, tuple(attempts))
        raise AssertionError("bounded attempt loop exhausted without returning")


class NativeYandexClient(_BaseYandexClient):
    endpoint = NATIVE_ENDPOINT

    def _payload(self, request: ModelRequest, model_uri: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "modelUri": model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": 0,
                "maxTokens": str(request.max_tokens),
                "reasoningOptions": {"mode": "DISABLED"},
            },
            "messages": [{"role": "user", "text": request.prompt}],
        }
        if request.schema is not None:
            payload["jsonSchema"] = {"schema": mutable_json(request.schema)}
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Api-Key {self.credentials.api_key}",
            "Content-Type": "application/json",
            "x-folder-id": self.credentials.folder_id,
        }

    def _parse(self, document: Mapping[str, object], role: ModelRole) -> _ParsedResponse:
        return _parse_native(document, role)


class YandexOpenAIClient(_BaseYandexClient):
    endpoint = OPENAI_ENDPOINT

    def _payload(self, request: ModelRequest, model_uri: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": model_uri,
            "stream": False,
            "temperature": 0,
            "max_tokens": request.max_tokens,
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "model_response",
                    "strict": True,
                    "schema": mutable_json(request.schema),
                },
            }
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Api-Key {self.credentials.api_key}",
            "Content-Type": "application/json",
            "OpenAI-Project": self.credentials.folder_id,
        }

    def _parse(self, document: Mapping[str, object], role: ModelRole) -> _ParsedResponse:
        return _parse_openai(document, role)


def normalize_model_uri(model: str, folder_id: str) -> str:
    return model if model.startswith("gpt://") else f"gpt://{folder_id}/{model}"
