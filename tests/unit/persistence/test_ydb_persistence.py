from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ydbdoc_review_ng.domain import Mode, ModelRole
from ydbdoc_review_ng.models import (
    AttemptError,
    AttemptResult,
    AttemptStatus,
    ModelRequest,
    ModelUsage,
)
from ydbdoc_review_ng.persistence import (
    DailyBudgetExceeded,
    JobStatus,
    PersistenceError,
    YdbPersistence,
)


class FakeExecutor:
    def __init__(self, rows: list[Mapping[str, object]] | None = None) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.rows = rows or []

    def execute(
        self, statement: str, parameters: Mapping[str, object], /
    ) -> list[Mapping[str, object]]:
        self.calls.append((statement, parameters))
        if "SUM(cost_rub)" in statement:
            return self.rows
        return []


class EchoingExecutor:
    def execute(
        self, _statement: str, parameters: Mapping[str, object], /
    ) -> list[Mapping[str, object]]:
        raise RuntimeError(f"YDB rejected bound parameters: {parameters!r}")


def attempt(
    *,
    cost: Decimal | None,
    raw_response: bytes | None = b'{"translation":"Ready"}',
    error: AttemptError | None = None,
) -> AttemptResult:
    request = ModelRequest(
        ModelRole.TRANSLATE,
        "yandexgpt-5.1/latest",
        "confidential source text",
        {"type": "object"},
    )
    return AttemptResult(
        attempt_number=1,
        request_role=ModelRole.TRANSLATE,
        request_model=request.model,
        request=request,
        request_payload=b'{"messages":["confidential source text"]}',
        started_at=datetime(2026, 9, 21, 8, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 21, 8, 1, tzinfo=UTC),
        status=AttemptStatus.FAILED if error else AttemptStatus.SUCCEEDED,
        error=error,
        http_status=None,
        raw_response=raw_response,
        response_status="final" if raw_response else None,
        response_model=request.model if raw_response else None,
        response_role="assistant" if raw_response else None,
        text="Ready" if raw_response else None,
        usage=ModelUsage(),
        cost_rub=cost,
    )


def test_install_schema_creates_ttl_protected_job_and_attempt_tables() -> None:
    executor = FakeExecutor()

    YdbPersistence(executor).install_schema()

    ddl = "\n".join(statement for statement, _ in executor.calls)
    assert len(executor.calls) == 3
    assert "jobs" in ddl
    assert "attempts" in ddl
    assert ddl.count('Interval("P14D")') == 3
    assert "request" in ddl and "response" in ddl
    assert "source_sha Utf8 NOT NULL" not in executor.calls[0][0]
    assert "job_id Utf8," in executor.calls[1][0]
    assert "ON created_at" in executor.calls[2][0]


def test_existing_schema_migration_keeps_audits_and_adds_nullable_binding() -> None:
    executor = FakeExecutor()
    YdbPersistence(executor).migrate_schema()
    statements = [statement for statement, _ in executor.calls]
    assert len(statements) == 3
    assert statements[0] == "ALTER TABLE `ydbdoc_review/attempts` ADD COLUMN job_id Utf8;"
    assert (
        statements[1] == "ALTER TABLE `ydbdoc_review/jobs` ALTER COLUMN source_sha DROP NOT NULL;"
    )
    assert "CREATE TABLE `ydbdoc_review/continuations`" in statements[2]
    assert 'TTL = Interval("P14D") ON created_at' in statements[2]


def test_migration_failure_does_not_echo_sdk_diagnostics() -> None:
    with pytest.raises(PersistenceError, match="schema migration") as error:
        YdbPersistence(EchoingExecutor()).migrate_schema()
    assert "bound parameters" not in str(error.value)


def test_continue_starts_without_sha_then_binds_restored_snapshot() -> None:
    executor = FakeExecutor()
    store = YdbPersistence(executor)
    job_id = store.start_job(
        Mode.DOC_CONTINUE,
        pr_number=52,
        source_sha=None,
        target_sha=None,
        started_at=datetime(2026, 9, 21, 9, tzinfo=UTC),
    )
    store.bind_job_snapshot(job_id, source_sha="a" * 40, target_sha="b" * 40)
    assert executor.calls[0][1]["source_sha"] is None
    query, bound = executor.calls[1]
    assert bound == {"job_id": job_id, "source_sha": "a" * 40, "target_sha": "b" * 40}
    assert "UPDATE" in query and "source_sha IS NULL" in query


def test_continue_budget_gate_performs_no_database_reads() -> None:
    executor = FakeExecutor()
    YdbPersistence(executor).check_daily_budget(
        Mode.DOC_CONTINUE, limit_rub=Decimal(0), now=datetime(2026, 9, 21, tzinfo=UTC)
    )
    assert executor.calls == []


def test_start_and_terminal_finish_write_a_job_audit_record() -> None:
    executor = FakeExecutor()
    store = YdbPersistence(executor)
    started_at = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
    finished_at = datetime(2026, 9, 21, 9, 5, tzinfo=UTC)

    job_id = store.start_job(
        Mode.DOC_TRANSLATE,
        pr_number=50123,
        source_sha="a" * 40,
        target_sha=None,
        started_at=started_at,
    )
    store.finish_job(job_id, JobStatus.FAILED, error="validation_failed", finished_at=finished_at)

    started = executor.calls[0][1]
    finished = executor.calls[1][1]
    assert started["mode"] == "doc_translate"
    assert started["pr_number"] == 50123
    assert started["source_sha"] == "a" * 40
    assert started["target_sha"] is None
    assert started["started_at"] == started_at
    assert started["status"] == "started"
    assert finished == {
        "job_id": job_id,
        "status": "failed",
        "error": "validation_failed",
        "finished_at": finished_at,
    }


def test_t017_f10_terminal_success_upserts_final_target_sha() -> None:
    executor = FakeExecutor()
    store = YdbPersistence(executor)
    final_sha = "f" * 40

    store.finish_job(
        "job-1",
        JobStatus.SUCCEEDED,
        error=None,
        finished_at=datetime(2026, 9, 21, 9, 5, tzinfo=UTC),
        target_sha=final_sha,
    )

    statement, parameters = executor.calls[0]
    assert "target_sha" in statement
    assert parameters["target_sha"] == final_sha


def test_attempt_recorder_stores_exact_response_and_known_nonzero_decimal_cost() -> None:
    executor = FakeExecutor()
    store = YdbPersistence(executor)
    result = attempt(cost=Decimal("1.2300"))

    store(result)

    params = executor.calls[0][1]
    assert params["role"] == "translate"
    assert params["request"] == b'{"messages":["confidential source text"]}'
    assert params["response"] == b'{"translation":"Ready"}'
    assert params["status"] == "succeeded"
    assert params["error"] is None
    assert params["model"] == "yandexgpt-5.1/latest"
    assert params["cost_rub"] == Decimal("1.2300")
    assert "confidential source text" not in repr(store)
    assert "Ready" not in repr(store)


def test_attempt_recorder_sanitizes_an_echoing_executor_error() -> None:
    store = YdbPersistence(EchoingExecutor())

    with pytest.raises(PersistenceError) as raised:
        store(attempt(cost=Decimal("1.23")))

    assert type(raised.value) is PersistenceError
    assert str(raised.value) == "YDB persistence failed during attempt recording"
    assert "confidential source text" not in str(raised.value)
    assert "translation" not in str(raised.value)


def test_attempt_without_response_or_billable_cost_stores_unknown_cost_as_null() -> None:
    executor = FakeExecutor()

    YdbPersistence(executor)(attempt(cost=None, raw_response=None, error=AttemptError.TRANSPORT))

    params = executor.calls[0][1]
    assert params["response"] is None
    assert params["cost_rub"] is None
    assert params["error"] == "transport"


def test_attempt_with_trusted_zero_cost_keeps_zero_known() -> None:
    executor = FakeExecutor()

    YdbPersistence(executor)(attempt(cost=Decimal(0)))

    assert executor.calls[0][1]["cost_rub"] == Decimal(0)


@pytest.mark.parametrize(
    ("day", "expected_start", "expected_end"),
    [
        (
            date(2010, 1, 15),
            datetime(2010, 1, 14, 21, 0, tzinfo=UTC),
            datetime(2010, 1, 15, 21, 0, tzinfo=UTC),
        ),
        (
            date(2010, 7, 15),
            datetime(2010, 7, 14, 20, 0, tzinfo=UTC),
            datetime(2010, 7, 15, 20, 0, tzinfo=UTC),
        ),
    ],
)
def test_known_cost_sum_uses_moscow_calendar_day_utc_interval(
    day: date, expected_start: datetime, expected_end: datetime
) -> None:
    executor = FakeExecutor([{"total_cost_rub": Decimal("2.50")}])

    total = YdbPersistence(executor).known_cost_for_moscow_date(day)

    statement, params = executor.calls[0]
    assert "SUM(cost_rub)" in statement
    assert "role" not in statement
    assert params == {"start_at": expected_start, "end_at": expected_end}
    assert total == Decimal("2.50")


def test_budget_blocks_translate_at_or_above_limit_with_exact_user_message() -> None:
    for total in (Decimal(10), Decimal("10.01")):
        executor = FakeExecutor([{"total_cost_rub": total}])

        with pytest.raises(DailyBudgetExceeded) as raised:
            YdbPersistence(executor).check_daily_budget(
                Mode.DOC_TRANSLATE,
                limit_rub=Decimal(10),
                now=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
            )

        assert str(raised.value) == "квота на сегодня исчерпана, попробуйте позже"
        assert len(executor.calls) == 1


def test_budget_allows_below_limit_and_verify_skips_budget_query() -> None:
    translate = FakeExecutor([{"total_cost_rub": Decimal("9.99")}])
    YdbPersistence(translate).check_daily_budget(
        Mode.DOC_TRANSLATE,
        limit_rub=Decimal(10),
        now=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )
    verify = FakeExecutor([{"total_cost_rub": Decimal(999)}])
    YdbPersistence(verify).check_daily_budget(
        Mode.DOC_VERIFY,
        limit_rub=Decimal(10),
        now=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )

    assert len(translate.calls) == 1
    assert verify.calls == []
