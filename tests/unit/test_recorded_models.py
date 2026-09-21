from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.unit.persistence.test_ydb_persistence import FakeExecutor, attempt
from ydbdoc_review_ng.domain import Mode
from ydbdoc_review_ng.models import AttemptError
from ydbdoc_review_ng.persistence import YdbPersistence
from ydbdoc_review_ng.runtime import RecordedModels


@pytest.mark.parametrize("cost", [None, Decimal(0), Decimal("1.25")])
def test_recorder_binds_actual_attempts_to_current_job_and_preserves_cost(cost) -> None:
    executor = FakeExecutor()
    store = YdbPersistence(executor)
    models = RecordedModels({}, store, object())
    job_id = store.start_job(
        Mode.DOC_CONTINUE,
        pr_number=42,
        source_sha=None,
        target_sha=None,
        started_at=datetime(2026, 9, 21, tzinfo=UTC),
    )
    models.bind_job(job_id)
    result = attempt(
        cost=cost,
        raw_response=None if cost is None else b"response",
        error=AttemptError.TRANSPORT if cost is None else None,
    )
    models.record(result)
    assert executor.calls[-1][1]["job_id"] == job_id
    assert executor.calls[-1][1]["cost_rub"] == cost
    assert models.cost == cost


def test_continue_costs_are_in_common_daily_sum() -> None:
    class CostExecutor(FakeExecutor):
        def execute(self, statement, parameters, /):
            if "SUM(cost_rub)" in statement:
                costs = [p["cost_rub"] for s, p in self.calls if "cost_rub" in p]
                self.rows = [{"total_cost_rub": sum(c for c in costs if c is not None)}]
            return super().execute(statement, parameters)

    executor = CostExecutor()
    store = YdbPersistence(executor)
    models = RecordedModels({}, store, object())
    models.bind_job("continue-job")
    for cost in (None, Decimal(0), Decimal("2.25")):
        models.record(attempt(cost=cost))
    assert store.known_cost_for_moscow_date(date(2026, 9, 21)) == Decimal("2.25")
    assert "job_id" not in executor.calls[-1][0]


def test_unbound_recorder_fails_before_model_transport() -> None:
    models = RecordedModels({}, YdbPersistence(FakeExecutor()), object())
    with pytest.raises(Exception, match="job"):
        models.invoke(attempt(cost=None).request)
