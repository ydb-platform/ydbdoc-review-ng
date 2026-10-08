"""Reuse the production provider, but require a reservation for every paid attempt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from math import isfinite

from ydbdoc_review_ng.models import (
    AttemptError,
    AttemptResult,
    ExecutionConfig,
    HttpTransport,
    ModelRequest,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.models.configuration import (
    DEEPSEEK_PRICE,
    PRODUCTION_MODEL,
    PRODUCTION_PRICING,
)
from ydbdoc_review_ng.policy_review.budget import RunBudget
from ydbdoc_review_ng.policy_review.types import ReviewError


@dataclass(frozen=True, slots=True)
class PolicyModelResult:
    text: str | None = field(repr=False)
    status: str


class BudgetedPolicyModel:
    def __init__(
        self,
        credentials: YandexCredentials,
        transport: HttpTransport,
        budget: RunBudget,
        record_attempt: Callable[[AttemptResult], None],
        *,
        timeout_seconds: float = 600,
        before_attempt: Callable[[], None] | None = None,
    ) -> None:
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ReviewError("invalid_model_timeout")
        self._credentials, self._transport = credentials, transport
        self.budget, self._record_attempt = budget, record_attempt
        self._timeout = timeout_seconds
        self._audit_failed = False
        self._before_attempt = before_attempt

    def invoke(self, request: ModelRequest) -> PolicyModelResult:
        if self._audit_failed:
            return PolicyModelResult(None, "model_audit_unavailable")
        if self._before_attempt is not None:
            try:
                self._before_attempt()
            except ReviewError as error:
                return PolicyModelResult(None, error.code)
        if request.model != PRODUCTION_MODEL or request.max_output_tokens is None:
            raise ReviewError("unsupported_policy_model")
        reservation: int | None = None
        recorded = False

        def record(attempt: AttemptResult) -> None:
            nonlocal recorded
            assert reservation is not None
            recorded = True
            self.budget.settle(reservation, attempt.cost_rub)
            try:
                self._record_attempt(attempt)
            except Exception:  # noqa: BLE001 - audit diagnostics may contain secrets.
                self._audit_failed = True
                raise ReviewError("model_audit_unavailable") from None

        client = YandexOpenAIClient(
            self._credentials, self._transport, record,
            pricing=PRODUCTION_PRICING,
            # Retries are owned by the reviewer so each one needs a new reservation.
            execution=ExecutionConfig(max_attempts=1),
            timeout_seconds=self._timeout,
        )
        try:
            wire = client.prepare_request(request)
        except ValueError:
            raise ReviewError("model_context_limit_exceeded") from None
        upper_bound = (
            Decimal(wire.input_tokens) * DEEPSEEK_PRICE.input_token_rub
            + Decimal(wire.max_tokens) * DEEPSEEK_PRICE.output_token_rub
        )
        try:
            reservation = self.budget.reserve(upper_bound)
        except ReviewError as error:
            return PolicyModelResult(None, error.code)
        try:
            result = client.invoke(request)
        except Exception:  # noqa: BLE001 - transport errors must not leak credentials.
            self._audit_failed = True
            # An interrupted/unknown transport can still be billable. Retain its reserve.
            if not recorded:
                self.budget.settle(reservation, None)
            raise ReviewError("model_attempt_interrupted") from None
        reason = self.budget.stop_reason()
        if reason is not None:
            return PolicyModelResult(None, reason)
        if result.success and result.text is not None:
            return PolicyModelResult(result.text, "ok")
        retryable = (
            result.failure in {AttemptError.MALFORMED_RESPONSE, AttemptError.NON_FINAL,
                               AttemptError.EMPTY_TEXT}
            or result.failure is AttemptError.HTTP_STATUS
            and result.attempts[-1].http_status in {429, 500, 502, 503, 504}
        )
        return PolicyModelResult(None, "retryable_model_error" if retryable else "model_error")
