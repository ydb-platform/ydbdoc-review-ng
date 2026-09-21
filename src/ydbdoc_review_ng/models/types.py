"""Immutable values for provider-neutral model execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Protocol, TypeAlias, cast

from ydbdoc_review_ng.domain import ModelRole

JsonScalar: TypeAlias = None | bool | int | float | str
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


def freeze_json(value: object) -> FrozenJson:
    if value is None or type(value) in {bool, int, float, str}:
        return cast(JsonScalar, value)
    if type(value) is list or type(value) is tuple:
        return tuple(freeze_json(item) for item in value)
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be exact strings")
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    raise TypeError("value is not JSON-compatible")


def mutable_json(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: mutable_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [mutable_json(item) for item in value]
    return value


class AttemptStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AttemptError(str, Enum):
    TRANSPORT = "transport"
    HTTP_STATUS = "http_status"
    MALFORMED_RESPONSE = "malformed_response"
    NON_FINAL = "non_final"
    EMPTY_TEXT = "empty_text"
    REASONING_NOT_DISABLED = "reasoning_not_disabled"
    UNSUPPORTED_MODEL = "unsupported_model"


@dataclass(frozen=True, slots=True)
class ModelRequest:
    role: ModelRole
    model: str
    prompt: str = field(repr=False)
    schema: FrozenJson = field(repr=False)
    max_tokens: int = 2000

    def __post_init__(self) -> None:
        if type(self.role) is not ModelRole:
            raise TypeError("role must be ModelRole")
        if type(self.model) is not str or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if type(self.prompt) is not str or not self.prompt:
            raise ValueError("prompt must be a non-empty string")
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        object.__setattr__(self, "schema", freeze_json(self.schema))


@dataclass(frozen=True, slots=True)
class YandexCredentials:
    api_key: str = field(repr=False)
    folder_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.api_key) is not str or not self.api_key:
            raise ValueError("api_key must be a non-empty string")
        if type(self.folder_id) is not str or not self.folder_id:
            raise ValueError("folder_id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    max_attempts: int = 2
    retryable_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if type(self.retryable_statuses) is not frozenset or any(
            type(status) is not int or status < 100 or status > 599
            for status in self.retryable_statuses
        ):
            raise ValueError("retryable_statuses must be a frozenset of HTTP statuses")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes = field(repr=False)
    billable_cost_rub: Decimal | None = None


class TransportFailure(RuntimeError):
    """A sanitized transport failure that never retains the provider's message."""

    def __init__(
        self,
        _unsafe_detail: object = None,
        *,
        retryable: bool,
        usage: ModelUsage | None = None,
        billable_cost_rub: Decimal | None = None,
        raw_response: bytes | None = None,
    ) -> None:
        self.retryable = retryable
        self.usage = usage or ModelUsage()
        self.billable_cost_rub = billable_cost_rub
        self.raw_response = raw_response
        super().__init__("model transport failed")


class HttpTransport(Protocol):
    def __call__(self, request: HttpRequest, /) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class ModelTokenPrice:
    input_token_rub: Decimal
    output_token_rub: Decimal
    reasoning_token_rub: Decimal

    def __post_init__(self) -> None:
        if any(
            type(value) is not Decimal or value < 0
            for value in (
                self.input_token_rub,
                self.output_token_rub,
                self.reasoning_token_rub,
            )
        ):
            raise ValueError("token prices must be non-negative Decimal values")


class CostCalculator(Protocol):
    def __call__(self, model: str, usage: ModelUsage, /) -> Decimal | None: ...


class PerModelPricing:
    """Decimal-safe cost calculator with no built-in provider prices."""

    __slots__ = ("_prices",)

    def __init__(self, prices: Mapping[str, ModelTokenPrice], /) -> None:
        self._prices = MappingProxyType(dict(prices))

    def __call__(self, model: str, usage: ModelUsage, /) -> Decimal | None:
        price = self._prices.get(model)
        if price is None or usage.input_tokens is None or usage.output_tokens is None:
            return None
        cost = Decimal(usage.input_tokens) * price.input_token_rub
        cost += Decimal(usage.output_tokens) * price.output_token_rub
        if usage.reasoning_tokens is not None:
            cost += Decimal(usage.reasoning_tokens) * price.reasoning_token_rub
        return cost


@dataclass(frozen=True, slots=True)
class AttemptResult:
    attempt_number: int
    request_role: ModelRole
    request_model: str
    request: ModelRequest = field(repr=False)
    request_payload: bytes = field(repr=False)
    started_at: datetime
    finished_at: datetime
    status: AttemptStatus
    error: AttemptError | None
    http_status: int | None
    raw_response: bytes | None = field(repr=False)
    response_status: str | None
    response_model: str | None
    response_role: str | None
    text: str | None = field(repr=False)
    usage: ModelUsage
    cost_rub: Decimal | None


AttemptRecorder: TypeAlias = Callable[[AttemptResult], None]


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    text: str | None = field(repr=False)
    failure: AttemptError | None
    attempts: tuple[AttemptResult, ...]

    @property
    def success(self) -> bool:
        return self.failure is None
