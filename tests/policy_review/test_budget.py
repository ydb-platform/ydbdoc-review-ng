from decimal import Decimal

import pytest

from ydbdoc_review_ng.policy_review.budget import BudgetEvent, RunBudget, run_limit
from ydbdoc_review_ng.policy_review.types import ReviewError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", [None, "", "nan", "sNaN", "Infinity", "-0.1", "secret-input"])
def test_invalid_limits_deny_without_echoing_input(value: str | None) -> None:
    with pytest.raises(ReviewError, match="^review_budget_missing_or_invalid$"):
        run_limit(value)


def test_reservations_bound_spend_across_attempts_without_float_rounding() -> None:
    events: list[BudgetEvent] = []
    budget = RunBudget(Decimal("0.3"), events.append)
    first = budget.reserve(Decimal("0.2"))
    with pytest.raises(ReviewError, match="budget_insufficient"):
        budget.reserve(Decimal("0.2"))
    budget.settle(first, Decimal("0.1"))
    budget.settle(budget.reserve(Decimal("0.2")), Decimal("0.2"))
    assert budget.spent == Decimal("0.3") and budget.reserved == 0
    with pytest.raises(ReviewError, match="budget_exceeded"):
        budget.reserve(Decimal("0.001"))
    assert [event.kind for event in events] == ["reserved", "settled", "reserved", "settled"]


def test_unknown_retains_reservation_and_denies_subsequent_paid_attempt() -> None:
    budget = RunBudget(Decimal(10), lambda _: None)
    budget.settle(budget.reserve(Decimal(2)), None)
    assert budget.unknown and budget.reserved == 2 and budget.spent == 0
    with pytest.raises(ReviewError, match="cost_unknown"):
        budget.reserve(Decimal(1))


def test_provider_cost_overrun_is_recorded_and_stops_following_attempts() -> None:
    budget = RunBudget(Decimal(2), lambda _: None)
    budget.settle(budget.reserve(Decimal(1)), Decimal(3))
    assert budget.spent == 3 and budget.stop_reason() == "budget_exceeded"
    with pytest.raises(ReviewError, match="budget_exceeded"):
        budget.reserve(Decimal(1))


def test_audit_failure_blocks_calls_and_is_not_rendered_as_free() -> None:
    def fail(_: BudgetEvent) -> None:
        raise RuntimeError("secret payload must not escape")
    budget = RunBudget(Decimal(10), fail)
    with pytest.raises(ReviewError, match="^budget_audit_unavailable$"):
        budget.reserve(Decimal(2))
    assert budget.reserved == 2
    with pytest.raises(ReviewError, match="budget_audit_unavailable"):
        budget.reserve(Decimal(1))


def test_settlement_cannot_be_replayed() -> None:
    budget = RunBudget(Decimal(10), lambda _: None)
    number = budget.reserve(Decimal(2))
    budget.settle(number, Decimal(1))
    with pytest.raises(ReviewError, match="unknown_budget_attempt"):
        budget.settle(number, Decimal(1))


def test_production_budget_variable_is_required_without_paid_default() -> None:
    with pytest.raises(ReviewError, match="review_budget_missing_or_invalid"):
        RunBudget.from_environment({}, lambda _: None)
    budget = RunBudget.from_environment({"YDBDOC_REVIEW_MAX_RUN_COST_RUB": "1.25"}, lambda _: None)
    assert budget.limit == Decimal("1.25")
