from decimal import Decimal

import pytest

from ydbdoc_review_ng.policy_review.budget import BudgetEvent
from ydbdoc_review_ng.policy_review.store import YdbReviewStore
from ydbdoc_review_ng.policy_review.types import ReviewError

pytestmark = pytest.mark.unit


class Executor:
    def __init__(self, rows=None):
        self.rows = [{"accepted": True}] if rows is None else rows
        self.calls = []

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        return self.rows


def test_claim_passes_operator_data_only_as_parameters_in_one_query() -> None:
    executor = Executor()
    owner = "owner'; DELETE FROM other; --"
    assert YdbReviewStore(executor).claim(42, "a" * 40, "manual:1", owner)
    assert len(executor.calls) == 1
    query, params = executor.calls[0]
    assert owner not in query and params["owner"] == owner


def test_rejected_atomic_reservation_is_not_accepted_as_an_audited_paid_call() -> None:
    store = YdbReviewStore(Executor([{"accepted": False}]))
    with pytest.raises(ReviewError, match="review_audit_conflict"):
        store.budget("owner", BudgetEvent("reserved", 1, Decimal(2)))


def test_sdk_failures_are_sanitized() -> None:
    class FailedExecutor:
        def execute(self, *args):
            raise RuntimeError("private credential payload")
    with pytest.raises(ReviewError, match="^review_audit_unavailable$"):
        YdbReviewStore(FailedExecutor()).approved(42, "a" * 40)


def test_recovery_retains_unknown_reserve_and_known_spend() -> None:
    class RecoveryExecutor:
        def execute(self, statement, parameters):
            if "doc_review_runs" in statement:
                return [{"report": b"{}"}]
            return [{"kind": "settled", "cost": Decimal("1.25"), "reservation": Decimal(2)},
                    {"kind": "reserved", "cost": None, "reservation": Decimal(3)}]
    report = YdbReviewStore(RecoveryExecutor()).recover("owner")
    assert report["status"] == "cost_unknown"
    assert report["cost_rub"] is None and report["reserved_rub"] == "3"


def test_cancel_recovery_with_only_known_cost_is_not_reported_as_free() -> None:
    class RecoveryExecutor:
        def execute(self, statement, parameters):
            if "doc_review_runs" in statement:
                return [{"report": b"{}"}]
            return [{"kind": "settled", "cost": Decimal("1.25"), "reservation": Decimal(2)}]
    report = YdbReviewStore(RecoveryExecutor()).recover("owner")
    assert report["status"] == "cancelled" and report["cost_rub"] == "1.25"
