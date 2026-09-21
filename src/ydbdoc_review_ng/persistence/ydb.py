"""YDB statements for short-lived audit rows.

The executor is deliberately a narrow injected boundary.  Connection setup,
transactions, retries, and workflow sequencing belong outside this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from ydbdoc_review_ng.continuation import (
    ContinuationStage,
    ContinuationState,
    SourceChangeInventory,
    decode_scope_target_paths,
    decode_source_inventory,
    decode_state,
    encode_scope_target_paths,
    encode_source_inventory,
    encode_state,
)
from ydbdoc_review_ng.domain import GitSha, Mode, RepoPath
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


class CheckpointStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"


def semantic_stop_error(stage: ContinuationStage) -> str:
    """The only audit errors that authorize a semantic continuation handoff."""
    return {
        ContinuationStage.DIRECTION: "continuable_direction",
        ContinuationStage.TRANSLATION: "continuable_translation",
        ContinuationStage.REVIEW: "continuable_review",
    }[stage]


@dataclass(frozen=True, slots=True)
class ContinuationCheckpoint:
    """One lineage's current semantic stop; its original timestamp never moves."""

    continuation_id: str
    job_id: str
    source_pr: int
    trigger_pr: int
    source_sha: GitSha
    base_sha: GitSha
    translation_branch: str
    target_sha: GitSha | None
    source_inventory: SourceChangeInventory
    scope_target_paths: tuple[RepoPath, ...]
    state: ContinuationState = field(repr=False)
    created_at: datetime
    status: CheckpointStatus = CheckpointStatus.OPEN

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value.strip()
            for value in (self.continuation_id, self.job_id, self.translation_branch)
        ) or any(
            type(value) is not int or value < 1 for value in (self.source_pr, self.trigger_pr)
        ):
            raise PersistenceError("invalid continuation envelope")
        if (
            type(self.source_sha) is not GitSha
            or type(self.base_sha) is not GitSha
            or (self.target_sha is not None and type(self.target_sha) is not GitSha)
            or type(self.status) is not CheckpointStatus
            or type(self.created_at) is not datetime
            or self.created_at.utcoffset() is None
        ):
            raise PersistenceError("invalid continuation envelope")
        try:
            encode_state(self.state)
            encode_source_inventory(self.source_inventory)
            encode_scope_target_paths(self.scope_target_paths)
        except (TypeError, ValueError):
            raise PersistenceError("invalid continuation state") from None
        if self.state.stage is ContinuationStage.REVIEW and self.target_sha is None:
            raise PersistenceError("review checkpoint requires target SHA")
        if (self.state.stage is ContinuationStage.DIRECTION) != (not self.scope_target_paths):
            raise PersistenceError("continuation scope selection incompatible with stage")
        referenced = (
            {item.target_path for item in self.state.accepted_maps}
            | set(self.state.pending_paths)
            | set(self.state.review_paths)
        )
        if not referenced.issubset(self.scope_target_paths):
            raise PersistenceError("continuation scope selection omits state paths")

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(days=14)


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
                source_sha Utf8,
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
                job_id Utf8,
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
        self._install_continuations()

    def migrate_schema(self) -> None:
        """Explicit one-time upgrade from the original jobs/attempts schema.

        Do not run after install_schema. DDL is non-transactional; a partial
        failure requires checking applied statements before retrying the upgrade.
        """
        self._execute(
            "schema migration",
            f"ALTER TABLE `{self._table('attempts')}` ADD COLUMN job_id Utf8;",
            {},
        )
        self._execute(
            "schema migration",
            f"ALTER TABLE `{self._table('jobs')}` ALTER COLUMN source_sha DROP NOT NULL;",
            {},
        )
        self._install_continuations()

    def _install_continuations(self) -> None:
        self._execute(
            "continuation schema installation",
            f"""CREATE TABLE `{self._table("continuations")}` (
                continuation_id Utf8 NOT NULL,
                job_id Utf8 NOT NULL,
                source_pr Uint64 NOT NULL,
                trigger_pr Uint64 NOT NULL,
                source_sha Utf8 NOT NULL,
                base_sha Utf8 NOT NULL,
                translation_branch Utf8 NOT NULL,
                target_sha Utf8,
                stage Utf8 NOT NULL,
                source_inventory String NOT NULL,
                scope_target_paths String NOT NULL,
                state String NOT NULL,
                status Utf8 NOT NULL,
                created_at Timestamp NOT NULL,
                PRIMARY KEY (continuation_id)
            ) WITH (TTL = {_TTL} ON created_at);""",
            {},
        )

    def save_checkpoint(
        self, checkpoint: ContinuationCheckpoint, /, *, now: datetime
    ) -> ContinuationCheckpoint:
        """Stage a non-resumable semantic stop without refreshing its lineage age."""
        rows = self._execute(
            "checkpoint lookup",
            f"SELECT * FROM `{self._table('continuations')}` WHERE continuation_id = $continuation_id;",
            {"continuation_id": checkpoint.continuation_id},
        )
        if rows:
            previous = self._checkpoint(rows[0])
            self._require_live(previous, now)
            if (
                previous.state.stage is not ContinuationStage.DIRECTION
                and previous.scope_target_paths != checkpoint.scope_target_paths
            ):
                raise PersistenceError("continuation lineage mismatch")
            for name in (
                "job_id",
                "source_pr",
                "source_sha",
                "base_sha",
                "translation_branch",
                "source_inventory",
            ):
                if getattr(previous, name) != getattr(checkpoint, name):
                    raise PersistenceError("continuation lineage mismatch")
            checkpoint = replace(checkpoint, created_at=previous.created_at)
        self._require_live(checkpoint, now)
        checkpoint = replace(checkpoint, status=CheckpointStatus.PENDING)
        self._execute(
            "checkpoint save",
            f"""UPSERT INTO `{self._table("continuations")}`
                (continuation_id, job_id, source_pr, trigger_pr, source_sha, base_sha,
                 translation_branch, target_sha, stage, source_inventory, scope_target_paths,
                 state, status, created_at)
                VALUES ($continuation_id, $job_id, $source_pr, $trigger_pr, $source_sha,
                 $base_sha, $translation_branch, $target_sha, $stage, $source_inventory,
                 $scope_target_paths, $state, $status, $created_at);""",
            self._checkpoint_values(checkpoint),
        )
        return checkpoint

    @staticmethod
    def _checkpoint_values(checkpoint: ContinuationCheckpoint) -> dict[str, object]:
        return {
            "continuation_id": checkpoint.continuation_id,
            "job_id": checkpoint.job_id,
            "source_pr": checkpoint.source_pr,
            "trigger_pr": checkpoint.trigger_pr,
            "source_sha": checkpoint.source_sha.value,
            "base_sha": checkpoint.base_sha.value,
            "translation_branch": checkpoint.translation_branch,
            "target_sha": None if checkpoint.target_sha is None else checkpoint.target_sha.value,
            "stage": checkpoint.state.stage.value,
            "source_inventory": encode_source_inventory(checkpoint.source_inventory).encode(
                "utf-8"
            ),
            "scope_target_paths": encode_scope_target_paths(checkpoint.scope_target_paths).encode(
                "utf-8"
            ),
            "state": encode_state(checkpoint.state).encode("utf-8"),
            "status": checkpoint.status.value,
            "created_at": checkpoint.created_at,
        }

    def activate_checkpoint(
        self, checkpoint: ContinuationCheckpoint, /, *, now: datetime
    ) -> ContinuationCheckpoint:
        """Open this exact pending stop after its semantic terminal audit was acknowledged."""
        pending = replace(checkpoint, status=CheckpointStatus.PENDING)
        opened = replace(checkpoint, status=CheckpointStatus.OPEN)
        current = self._checkpoint_by_id(checkpoint.continuation_id)
        self._require_live(current, now)
        if current not in (pending, opened):
            raise PersistenceError("checkpoint activation mismatch")
        self._validate_job(current)
        if current == pending:
            # A lost acknowledgement succeeds only when exact read-back proves
            # that this guarded activation and the semantic audit committed.
            with suppress(PersistenceError):
                self._execute(
                    "checkpoint activation",
                    f"""UPDATE `{self._table("continuations")}` SET status = 'open'
                        WHERE continuation_id = $continuation_id AND status = $status
                        AND job_id = $job_id AND source_pr = $source_pr AND trigger_pr = $trigger_pr
                        AND source_sha = $source_sha AND base_sha = $base_sha
                        AND translation_branch = $translation_branch AND created_at = $created_at
                        AND (target_sha = $target_sha OR (target_sha IS NULL AND $target_sha IS NULL))
                        AND stage = $stage AND state = $state
                        AND source_inventory = $source_inventory
                        AND scope_target_paths = $scope_target_paths;""",
                    self._checkpoint_values(pending),
                )
        actual = self._checkpoint_by_id(checkpoint.continuation_id)
        if actual != opened:
            raise PersistenceError("checkpoint activation unconfirmed")
        self._require_open(actual, now)
        self.validate_checkpoint_job(actual)
        return actual

    def _checkpoint_by_id(self, continuation_id: str) -> ContinuationCheckpoint:
        rows = self._execute(
            "checkpoint lookup",
            f"SELECT * FROM `{self._table('continuations')}` WHERE continuation_id = $continuation_id;",
            {"continuation_id": continuation_id},
        )
        if len(rows) != 1:
            raise PersistenceError("continuation checkpoint missing or ambiguous")
        return self._checkpoint(rows[0])

    def validate_checkpoint_job(self, checkpoint: ContinuationCheckpoint, /) -> None:
        """Reject non-open records or an original audit without the exact semantic marker."""
        if checkpoint.status is not CheckpointStatus.OPEN:
            raise PersistenceError("continuation checkpoint is not open")
        self._validate_job(checkpoint)

    def _validate_job(self, checkpoint: ContinuationCheckpoint) -> None:
        rows = self._execute(
            "checkpoint job lookup",
            f"SELECT * FROM `{self._table('jobs')}` WHERE job_id = $job_id;",
            {"job_id": checkpoint.job_id},
        )
        expected = {
            "job_id": checkpoint.job_id,
            "status": JobStatus.FAILED.value,
            "error": semantic_stop_error(checkpoint.state.stage),
            "source_sha": checkpoint.source_sha.value,
            "target_sha": None if checkpoint.target_sha is None else checkpoint.target_sha.value,
        }
        if len(rows) != 1 or any(rows[0].get(key) != value for key, value in expected.items()):
            raise PersistenceError("continuation original job is not a matching semantic stop")

    def load_checkpoint(self, pr_number: int, /, *, now: datetime) -> ContinuationCheckpoint:
        rows = self._execute(
            "checkpoint lookup",
            f"""SELECT * FROM `{self._table("continuations")}`
                WHERE (source_pr = $pr_number OR trigger_pr = $pr_number) AND status = 'open';""",
            {"pr_number": pr_number},
        )
        applicable = [self._checkpoint(row) for row in rows if row.get("status") == "open"]
        if len(applicable) != 1:
            raise PersistenceError("continuation checkpoint missing or ambiguous")
        checkpoint = applicable[0]
        self._require_open(checkpoint, now)
        self.validate_checkpoint_job(checkpoint)
        return checkpoint

    def close_checkpoint(self, continuation_id: str, /) -> None:
        self._execute(
            "checkpoint close",
            f"UPDATE `{self._table('continuations')}` SET status = 'closed' WHERE continuation_id = $continuation_id;",
            {"continuation_id": continuation_id},
        )

    @staticmethod
    def _require_open(checkpoint: ContinuationCheckpoint, now: datetime) -> None:
        YdbPersistence._require_live(checkpoint, now)
        if checkpoint.status is not CheckpointStatus.OPEN:
            raise PersistenceError("continuation checkpoint is not open")

    @staticmethod
    def _require_live(checkpoint: ContinuationCheckpoint, now: datetime) -> None:
        if (
            now.utcoffset() is None
            or checkpoint.status is CheckpointStatus.CLOSED
            or not (checkpoint.created_at <= now < checkpoint.expires_at)
        ):
            raise PersistenceError("continuation checkpoint closed or expired")

    @staticmethod
    def _checkpoint(row: Mapping[str, object]) -> ContinuationCheckpoint:
        try:
            state = decode_state(cast(str | bytes, row["state"]))
            if row["stage"] != state.stage.value:
                raise ValueError
            return ContinuationCheckpoint(
                continuation_id=cast(str, row["continuation_id"]),
                job_id=cast(str, row["job_id"]),
                source_pr=cast(int, row["source_pr"]),
                trigger_pr=cast(int, row["trigger_pr"]),
                source_sha=GitSha(cast(str, row["source_sha"])),
                base_sha=GitSha(cast(str, row["base_sha"])),
                source_inventory=decode_source_inventory(
                    cast(str | bytes, row["source_inventory"])
                ),
                scope_target_paths=decode_scope_target_paths(
                    cast(str | bytes, row["scope_target_paths"])
                ),
                translation_branch=cast(str, row["translation_branch"]),
                target_sha=None
                if row["target_sha"] is None
                else GitSha(cast(str, row["target_sha"])),
                state=state,
                created_at=cast(datetime, row["created_at"]),
                status=CheckpointStatus(cast(str, row["status"])),
            )
        except (KeyError, TypeError, ValueError):
            raise PersistenceError("invalid continuation checkpoint") from None

    def start_job(
        self,
        mode: Mode,
        /,
        *,
        pr_number: int,
        source_sha: str | None,
        target_sha: str | None,
        started_at: datetime,
    ) -> str:
        self._require_audited_mode(mode)
        if source_sha is None and mode is not Mode.DOC_CONTINUE:
            raise ValueError("source SHA is required for translate and verify")
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

    def bind_job_snapshot(self, job_id: str, /, *, source_sha: str, target_sha: str | None) -> None:
        """Bind a restored continue snapshot without replacing conflicting SHA values."""
        try:
            GitSha(source_sha)
            if target_sha is not None:
                GitSha(target_sha)
        except (TypeError, ValueError):
            raise PersistenceError("invalid job snapshot") from None
        self._execute(
            "job snapshot binding",
            f"""UPDATE `{self._table("jobs")}` SET source_sha = $source_sha, target_sha = $target_sha
                WHERE job_id = $job_id AND mode = 'doc_continue' AND status = 'started'
                AND (source_sha IS NULL OR source_sha = $source_sha)
                AND (target_sha IS NULL OR target_sha = $target_sha);""",
            {"job_id": job_id, "source_sha": source_sha, "target_sha": target_sha},
        )

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

    def __call__(self, attempt: AttemptResult, /, *, job_id: str | None = None) -> None:
        """Record a started model attempt through the T010 ``AttemptRecorder`` shape."""
        self._execute(
            "attempt recording",
            f"""UPSERT INTO `{self._table("attempts")}`
                (attempt_id, job_id, role, request, response, status, error, model, started_at,
                 finished_at, cost_rub)
                VALUES ($attempt_id, $job_id, $role, $request, $response, $status, $error, $model,
                 $started_at, $finished_at, $cost_rub);""",
            {
                "attempt_id": str(uuid4()),
                "job_id": job_id,
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
        if mode in {Mode.DOC_VERIFY, Mode.DOC_CONTINUE}:
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
        if mode not in {Mode.DOC_TRANSLATE, Mode.DOC_VERIFY, Mode.DOC_CONTINUE}:
            raise ValueError("unsupported audit mode")

    @staticmethod
    def _moscow_day_interval(day: date) -> tuple[datetime, datetime]:
        start_local = datetime.combine(day, time.min, tzinfo=_MOSCOW)
        end_local = datetime.combine(day.fromordinal(day.toordinal() + 1), time.min, tzinfo=_MOSCOW)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)
