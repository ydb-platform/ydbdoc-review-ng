"""Sequential per-attempt money reservations, including unknown billable outcomes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import Lock

from ydbdoc_review_ng.policy_review.types import ReviewError


def money(value: object) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise ReviewError("invalid_cost")
    return value


def run_limit(raw: str | None) -> Decimal:
    if not raw or len(raw) > 64:
        raise ReviewError("review_budget_missing_or_invalid")
    try:
        return money(Decimal(raw))
    except (InvalidOperation, ReviewError):
        raise ReviewError("review_budget_missing_or_invalid") from None


@dataclass(frozen=True, slots=True)
class BudgetEvent:
    kind: str
    attempt_id: int
    reservation_rub: Decimal
    cost_rub: Decimal | None = None


class RunBudget:
    """Audit must durably accept each reservation before any paid call can begin."""

    def __init__(self, limit: Decimal, audit: Callable[[BudgetEvent], None]) -> None:
        self.limit = money(limit)
        self.spent = Decimal(0)
        self.unknown = False
        self._audit = audit
        self._lock = Lock()
        self._next = 1
        self._pending: dict[int, Decimal] = {}
        self._audit_failed = False

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str], audit: Callable[[BudgetEvent], None],
    ) -> RunBudget:
        return cls(run_limit(environment.get("YDBDOC_REVIEW_MAX_RUN_COST_RUB")), audit)

    @property
    def reserved(self) -> Decimal:
        return sum(self._pending.values(), Decimal(0))

    def _record(self, event: BudgetEvent) -> None:
        try:
            self._audit(event)
        except Exception:  # noqa: BLE001 - audit errors may contain credentials or payloads.
            self._audit_failed = True
            raise ReviewError("budget_audit_unavailable") from None

    def reserve(self, upper_bound: Decimal) -> int:
        amount = money(upper_bound)
        with self._lock:
            if self._audit_failed:
                raise ReviewError("budget_audit_unavailable")
            if self.unknown:
                raise ReviewError("cost_unknown")
            if self.spent >= self.limit:
                raise ReviewError("budget_exceeded")
            if self.spent + self.reserved + amount > self.limit:
                raise ReviewError("budget_insufficient")
            number = self._next
            self._next += 1
            self._pending[number] = amount
            self._record(BudgetEvent("reserved", number, amount))
            return number

    def settle(self, attempt_id: int, cost: Decimal | None) -> None:
        with self._lock:
            if attempt_id not in self._pending:
                raise ReviewError("unknown_budget_attempt")
            reservation = self._pending[attempt_id]
            if cost is None:
                self.unknown = True
                self._record(BudgetEvent("unknown", attempt_id, reservation))
                return
            try:
                checked = money(cost)
            except ReviewError:
                self.unknown = True
                self._record(BudgetEvent("unknown", attempt_id, reservation))
                raise
            self.spent += checked
            del self._pending[attempt_id]
            self._record(BudgetEvent("settled", attempt_id, reservation, checked))

    def stop_reason(self) -> str | None:
        if self._audit_failed:
            return "budget_audit_unavailable"
        if self.unknown:
            return "cost_unknown"
        if self.spent > self.limit:
            return "budget_exceeded"
        return None
