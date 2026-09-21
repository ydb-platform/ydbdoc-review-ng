"""YDB statements for short-lived audit rows.

The executor is deliberately a narrow injected boundary.  Connection setup,
transactions, retries, and workflow sequencing belong outside this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from ydbdoc_review_ng.domain import Mode
from ydbdoc_review_ng.models import AttemptResult

_MOSCOW = ZoneInfo("Europe/Moscow")
_TTL = 'Interval("P14D")'


class YdbExecutor(Protocol):
    def execute(
        self, statement: str, parameters: Mapping[str, object], /
    ) -> Sequence[Mapping[str, object]]: ...


class PersistenceError(RuntimeError):
    """A persistence error safe to show to a workflow caller."""


class DailyBudgetExceeded(PersistenceError):
    """Known daily translation costs have reached the configured limit."""

    user_message = "квота на сегодня исчерпана, попробуйте позже"

    def __init__(self) -> None:
        super().__init__(self.user_message)


class JobStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class YdbPersistence:
    """Store job/attempt audit data and expose the non-atomic daily gate."""

    __slots__ = ("_executor", "_prefix")

    def __init__(self, executor: YdbExecutor, /, *, table_prefix: str = "ydbdoc_review") -> None:
        if not table_prefix or any(character.isspace() for character in table_prefix):
            raise ValueError("table_prefix must be a non-empty path without whitespace")
        self._executor = executor
        self._prefix = table_prefix.rstrip("/")

    def install_schema(self) -> None:
        self._execute(
            "schema installation",
            f"""CREATE TABLE `{self._table("jobs")}` (
                job_id Utf8 NOT NULL,
                mode Utf8 NOT NULL,
                pr_number Uint64 NOT NULL,
                source_sha Utf8 NOT NULL,
                target_sha Utf8,
                started_at Timestamp NOT NULL,
                finished_at Timestamp,
                status Utf8 NOT NULL,
                error Utf8,
                PRIMARY KEY (job_id)
            ) WITH (TTL = {_TTL} ON started_at);""",
            {},
        )
        self._execute(
            "schema installation",
            f"""CREATE TABLE `{self._table("attempts")}` (
                attempt_id Utf8 NOT NULL,
                role Utf8 NOT NULL,
                request String NOT NULL,
                response String,
                status Utf8 NOT NULL,
                error Utf8,
                model Utf8 NOT NULL,
                started_at Timestamp NOT NULL,
                finished_at Timestamp NOT NULL,
                cost_rub Decimal(22,9),
                PRIMARY KEY (attempt_id)
            ) WITH (TTL = {_TTL} ON started_at);""",
            {},
        )

    def start_job(
        self,
        mode: Mode,
        /,
        *,
        pr_number: int,
        source_sha: str,
        target_sha: str | None,
        started_at: datetime,
    ) -> str:
        self._require_audited_mode(mode)
        job_id = str(uuid4())
        self._execute(
            "job start",
            f"""UPSERT INTO `{self._table("jobs")}`
                (job_id, mode, pr_number, source_sha, target_sha, started_at, status)
                VALUES ($job_id, $mode, $pr_number, $source_sha, $target_sha, $started_at, $status);""",
            {
                "job_id": job_id,
                "mode": mode.value,
                "pr_number": pr_number,
                "source_sha": source_sha,
                "target_sha": target_sha,
                "started_at": started_at,
                "status": JobStatus.STARTED.value,
            },
        )
        return job_id

    def finish_job(
        self,
        job_id: str,
        status: JobStatus,
        /,
        *,
        error: str | None,
        finished_at: datetime,
        target_sha: str | None = None,
    ) -> None:
        if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
            raise ValueError("job status must be terminal")
        parameters: dict[str, object] = {
            "job_id": job_id,
            "status": status.value,
            "error": error,
            "finished_at": finished_at,
        }
        if target_sha is None:
            statement = f"""UPSERT INTO `{self._table("jobs")}`
                (job_id, finished_at, status, error)
                VALUES ($job_id, $finished_at, $status, $error);"""
        else:
            parameters["target_sha"] = target_sha
            statement = f"""UPSERT INTO `{self._table("jobs")}`
                (job_id, target_sha, finished_at, status, error)
                VALUES ($job_id, $target_sha, $finished_at, $status, $error);"""
        self._execute("job finish", statement, parameters)

    def __call__(self, attempt: AttemptResult, /) -> None:
        """Record a started model attempt through the T010 ``AttemptRecorder`` shape."""
        self._execute(
            "attempt recording",
            f"""UPSERT INTO `{self._table("attempts")}`
                (attempt_id, role, request, response, status, error, model, started_at,
                 finished_at, cost_rub)
                VALUES ($attempt_id, $role, $request, $response, $status, $error, $model,
                 $started_at, $finished_at, $cost_rub);""",
            {
                "attempt_id": str(uuid4()),
                "role": attempt.request_role.value,
                "request": attempt.request_payload,
                "response": attempt.raw_response,
                "status": attempt.status.value,
                "error": attempt.error.value if attempt.error is not None else None,
                "model": attempt.request_model,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "cost_rub": attempt.cost_rub,
            },
        )

    def known_cost_for_moscow_date(self, day: date, /) -> Decimal:
        start_at, end_at = self._moscow_day_interval(day)
        rows = self._execute(
            "known-cost lookup",
            f"""SELECT SUM(cost_rub) AS total_cost_rub
                FROM `{self._table("attempts")}`
                WHERE started_at >= $start_at AND started_at < $end_at;""",
            {"start_at": start_at, "end_at": end_at},
        )
        if not rows or rows[0].get("total_cost_rub") is None:
            return Decimal(0)
        total = rows[0]["total_cost_rub"]
        if type(total) is not Decimal:
            raise PersistenceError("YDB returned an invalid known-cost sum")
        return total

    def check_daily_budget(self, mode: Mode, /, *, limit_rub: Decimal, now: datetime) -> None:
        if mode is Mode.DOC_VERIFY:
            return
        self._require_audited_mode(mode)
        if type(limit_rub) is not Decimal or limit_rub < 0:
            raise ValueError("limit_rub must be a non-negative Decimal")
        if self.known_cost_for_moscow_date(now.astimezone(_MOSCOW).date()) >= limit_rub:
            raise DailyBudgetExceeded()

    def _table(self, name: str) -> str:
        return f"{self._prefix}/{name}"

    def _execute(
        self, operation: str, statement: str, parameters: Mapping[str, object], /
    ) -> Sequence[Mapping[str, object]]:
        try:
            return self._executor.execute(statement, parameters)
        except DailyBudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 - executor diagnostics can contain request/response bytes.
            raise PersistenceError(f"YDB persistence failed during {operation}") from None

    @staticmethod
    def _require_audited_mode(mode: Mode) -> None:
        if mode not in {Mode.DOC_TRANSLATE, Mode.DOC_VERIFY}:
            raise ValueError("mode must be doc_translate or doc_verify")

    @staticmethod
    def _moscow_day_interval(day: date) -> tuple[datetime, datetime]:
        start_local = datetime.combine(day, time.min, tzinfo=_MOSCOW)
        end_local = datetime.combine(day.fromordinal(day.toordinal() + 1), time.min, tzinfo=_MOSCOW)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)
