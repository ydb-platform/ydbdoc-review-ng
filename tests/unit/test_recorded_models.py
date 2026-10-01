import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.unit.models.test_clients import FakeTransport, openai_response
from tests.unit.persistence.test_ydb_persistence import FakeExecutor, attempt
from tests.unit.test_runtime_content_translation import (
    content_filter_witness,
    content_with,
    document_for,
)
from ydbdoc_review_ng.domain import Mode, ModelRole, RepoPath
from ydbdoc_review_ng.models import AttemptError, HttpResponse, ModelRequest, TransportFailure
from ydbdoc_review_ng.persistence import YdbPersistence
from ydbdoc_review_ng.runtime import RecordedModels
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError


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


@pytest.mark.parametrize("second_status", ["stop", "content_filter", "length", "transport"])
def test_content_filter_retry_is_identical_bounded_and_audited(second_status) -> None:
    executor = FakeExecutor()
    first = HttpResponse(
        200, openai_response(status="content_filter").body, billable_cost_rub=Decimal(0)
    )
    second = (
        TransportFailure(retryable=True)
        if second_status == "transport"
        else openai_response(status=second_status)
    )
    transport = FakeTransport(first, second, openai_response())
    models = RecordedModels(
        {"YANDEX_API_KEY": "test", "YANDEX_FOLDER_ID": "folder"},
        YdbPersistence(executor), transport,
    )
    models.bind_job("job")
    result = models.invoke(ModelRequest(
        ModelRole.TRANSLATE, "deepseek-v4-flash", "translate", None,
        expected_response={"field-1": "Hello"},
        target_path=RepoPath("ydb/docs/en/core/page.md"),
    ))

    assert result.success is (second_status == "stop")
    assert len(transport.requests) == len(result.attempts) == len(executor.calls) == 2
    assert transport.requests[0] == transport.requests[1]
    assert [a.attempt_number for a in result.attempts] == [1, 2]
    rows = [parameters for _, parameters in executor.calls]
    assert [row["cost_rub"] for row in rows] == [
        Decimal(0), None if second_status == "transport" else Decimal("0.029"),
    ]
    for row in rows:
        assert row["target_path"] == "ydb/docs/en/core/page.md"
        assert row["model"] == "deepseek-v4-flash"
        assert row["job_id"] == "job"
        assert json.loads(row["request"])["model"] == "gpt://folder/deepseek-v4-flash"


def test_non_final_has_one_persisted_attempt() -> None:
    executor = FakeExecutor()
    transport = FakeTransport(openai_response(status="length"), openai_response())
    models = RecordedModels(
        {"YANDEX_API_KEY": "test", "YANDEX_FOLDER_ID": "folder"},
        YdbPersistence(executor), transport,
    )
    models.bind_job("job")
    result = models.invoke(ModelRequest(ModelRole.CRITIC, "deepseek-v4-flash", "review", None))
    assert result.failure is AttemptError.NON_FINAL
    assert len(executor.calls) == len(transport.requests) == 1


def test_filtered_translation_stops_after_two_audit_rows_without_fallback_or_split() -> None:
    executor = FakeExecutor()
    filtered = openai_response(status="content_filter")
    transport = FakeTransport(filtered, filtered, openai_response())
    models = RecordedModels(
        {"YANDEX_API_KEY": "test", "YANDEX_FOLDER_ID": "folder"},
        YdbPersistence(executor), transport,
    )
    models.bind_job("job")
    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document_for(content_filter_witness()))
    assert len(transport.requests) == len(executor.calls) == 2
    assert transport.requests[0] == transport.requests[1]
    assert all(row["model"] == "deepseek-v4-flash" for _, row in executor.calls)
    assert all(row["target_path"] == "ydb/docs/en/core/page.md" for _, row in executor.calls)


@pytest.mark.parametrize("cost", [None, Decimal(0)])
def test_malformed_usage_keeps_unknown_or_explicit_zero_audit_cost(cost) -> None:
    executor = FakeExecutor()
    payload = json.loads(openai_response().body)
    payload["usage"] = {"prompt_tokens": "invalid", "completion_tokens": -1}
    transport = FakeTransport(HttpResponse(200, json.dumps(payload).encode(), cost))
    models = RecordedModels(
        {"YANDEX_API_KEY": "test", "YANDEX_FOLDER_ID": "folder"},
        YdbPersistence(executor), transport,
    )
    models.bind_job("job")
    result = models.invoke(ModelRequest(ModelRole.TRANSLATE, "deepseek-v4-flash", "text", None))
    assert len(executor.calls) == 1
    assert result.attempts[0].usage.input_tokens is None
    assert result.attempts[0].usage.output_tokens is None
    assert executor.calls[0][1]["cost_rub"] == models.cost == cost
