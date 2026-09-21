"""Straight-line orchestration over already implemented component boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import NoReturn, Protocol

from ydbdoc_review_ng.domain import GitSha, Mode
from ydbdoc_review_ng.persistence import DailyBudgetExceeded, JobStatus
from ydbdoc_review_ng.ports import Clock
from ydbdoc_review_ng.quality import QualityReviewResult, Verdict


class WorkflowStage(str, Enum):
    AUDIT_START = "audit_start"
    AUTHORIZE = "authorize"
    SNAPSHOT = "snapshot"
    BUDGET = "budget"
    PREPARE = "prepare"
    LOAD_CANDIDATE = "load_candidate"
    VALIDATE = "validate"
    PUBLISH = "publish"
    REVIEW = "review"
    REPORT = "report"
    TERMINAL_AUDIT = "terminal_audit"


class WorkflowError(RuntimeError):
    """A stage-only workflow failure that cannot retain boundary payloads."""

    def __init__(self, mode: Mode, stage: WorkflowStage, /) -> None:
        self.mode = mode
        self.stage = stage
        super().__init__(f"{mode.value} workflow failed during {stage.value}")


def _require_pr_number(value: object, type_name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{type_name}.pr_number must be a positive integer")


@dataclass(frozen=True, slots=True)
class TranslateWorkflowInput:
    pr_number: int
    source_sha: GitSha
    budget_limit_rub: Decimal

    def __post_init__(self) -> None:
        _require_pr_number(self.pr_number, type(self).__name__)
        if type(self.source_sha) is not GitSha:
            raise TypeError("TranslateWorkflowInput.source_sha must be GitSha")
        if type(self.budget_limit_rub) is not Decimal or self.budget_limit_rub < 0:
            raise ValueError("TranslateWorkflowInput.budget_limit_rub must be non-negative Decimal")


@dataclass(frozen=True, slots=True)
class VerifyWorkflowInput:
    pr_number: int
    source_sha: GitSha
    target_sha: GitSha

    def __post_init__(self) -> None:
        _require_pr_number(self.pr_number, type(self).__name__)
        if type(self.source_sha) is not GitSha:
            raise TypeError("VerifyWorkflowInput.source_sha must be GitSha")
        if type(self.target_sha) is not GitSha:
            raise TypeError("VerifyWorkflowInput.target_sha must be GitSha")


@dataclass(frozen=True, slots=True)
class AuthorizedRun:
    mode: Mode
    branch: str
    current_target_sha: GitSha | None
    context: object = field(repr=False)

    def __post_init__(self) -> None:
        if self.mode not in {Mode.DOC_TRANSLATE, Mode.DOC_VERIFY}:
            raise ValueError("AuthorizedRun.mode must be doc_translate or doc_verify")
        if type(self.branch) is not str or not self.branch.strip():
            raise ValueError("AuthorizedRun.branch must be a non-empty string")
        if self.current_target_sha is not None and type(self.current_target_sha) is not GitSha:
            raise TypeError("AuthorizedRun.current_target_sha must be GitSha or None")
        if self.mode is Mode.DOC_VERIFY and self.current_target_sha is None:
            raise ValueError("AuthorizedRun.current_target_sha is required for doc_verify")


@dataclass(frozen=True, slots=True)
class ImmutableRunSnapshot:
    mode: Mode
    source_sha: GitSha
    target_sha: GitSha | None
    branch: str
    context: object = field(repr=False)

    def __post_init__(self) -> None:
        if self.mode not in {Mode.DOC_TRANSLATE, Mode.DOC_VERIFY}:
            raise ValueError("ImmutableRunSnapshot.mode must be doc_translate or doc_verify")
        if type(self.source_sha) is not GitSha:
            raise TypeError("ImmutableRunSnapshot.source_sha must be GitSha")
        if self.target_sha is not None and type(self.target_sha) is not GitSha:
            raise TypeError("ImmutableRunSnapshot.target_sha must be GitSha or None")
        if type(self.branch) is not str or not self.branch.strip():
            raise ValueError("ImmutableRunSnapshot.branch must be a non-empty string")
        if self.mode is Mode.DOC_VERIFY and self.target_sha is None:
            raise ValueError("ImmutableRunSnapshot.target_sha is required for doc_verify")


@dataclass(frozen=True, slots=True)
class WorkflowCandidate:
    content: bytes = field(repr=False)
    review_context: object = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise TypeError("WorkflowCandidate.content must be bytes")


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    job_id: str
    mode: Mode
    final_commit_sha: GitSha
    verdict: Verdict
    repair_applied: bool


class WorkflowPersistencePort(Protocol):
    def start_job(
        self,
        mode: Mode,
        /,
        *,
        pr_number: int,
        source_sha: str,
        target_sha: str | None,
        started_at: datetime,
    ) -> str: ...

    def finish_job(
        self,
        job_id: str,
        status: JobStatus,
        /,
        *,
        error: str | None,
        finished_at: datetime,
        target_sha: str | None = None,
    ) -> None: ...

    def check_daily_budget(self, mode: Mode, /, *, limit_rub: Decimal, now: datetime) -> None: ...


class SourceWorkflowPort(Protocol):
    def authorize_translate(self, request: TranslateWorkflowInput, /) -> AuthorizedRun: ...

    def snapshot_translate(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot: ...

    def authorize_verify(self, request: VerifyWorkflowInput, /) -> AuthorizedRun: ...

    def snapshot_verify(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot: ...


class ContentWorkflowPort(Protocol):
    def prepare_translation(self, snapshot: ImmutableRunSnapshot, /) -> WorkflowCandidate: ...

    def load_verification_candidate(
        self, snapshot: ImmutableRunSnapshot, /
    ) -> WorkflowCandidate: ...

    def validate_candidate(
        self, snapshot: ImmutableRunSnapshot, candidate: WorkflowCandidate, /
    ) -> None: ...


class QualityReviewPort(Protocol):
    def review(
        self,
        snapshot: ImmutableRunSnapshot,
        candidate: WorkflowCandidate,
        /,
        *,
        before_final_critic: Callable[[bytes], None] | None = None,
    ) -> QualityReviewResult: ...


class PublicationPort(Protocol):
    def publish(
        self, snapshot: ImmutableRunSnapshot, candidate: WorkflowCandidate, /
    ) -> GitSha: ...


class VerdictPort(Protocol):
    def update_current_pr(
        self,
        *,
        mode: Mode,
        pr_number: int,
        branch: str,
        commit_sha: GitSha,
        review: QualityReviewResult,
    ) -> None: ...


class LinearWorkflows:
    """Execute doc_translate and doc_verify as bounded linear workflows."""

    __slots__ = (
        "_clock",
        "_content",
        "_persistence",
        "_publisher",
        "_reporter",
        "_reviewer",
        "_source",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        persistence: WorkflowPersistencePort,
        source: SourceWorkflowPort,
        content: ContentWorkflowPort,
        reviewer: QualityReviewPort,
        publisher: PublicationPort,
        reporter: VerdictPort,
    ) -> None:
        self._clock = clock
        self._persistence = persistence
        self._source = source
        self._content = content
        self._reviewer = reviewer
        self._publisher = publisher
        self._reporter = reporter

    def doc_translate(self, request: TranslateWorkflowInput, /) -> WorkflowResult:
        mode = Mode.DOC_TRANSLATE
        job_id, audit_started_at = self._start_job(
            mode,
            request.pr_number,
            request.source_sha,
            target_sha=None,
        )
        final_sha: GitSha | None = None
        stage = WorkflowStage.AUTHORIZE
        try:
            authorization = self._source.authorize_translate(request)
            self._require_mode(authorization.mode, mode)
            stage = WorkflowStage.SNAPSHOT
            snapshot = self._source.snapshot_translate(authorization)
            self._require_snapshot(snapshot, authorization, mode)
            stage = WorkflowStage.BUDGET
            self._persistence.check_daily_budget(
                mode,
                limit_rub=request.budget_limit_rub,
                now=self._clock.now(),
            )
            stage = WorkflowStage.PREPARE
            candidate = self._content.prepare_translation(snapshot)
            stage = WorkflowStage.VALIDATE
            self._content.validate_candidate(snapshot, candidate)
            stage = WorkflowStage.PUBLISH
            final_sha = self._publisher.publish(snapshot, candidate)
            stage = WorkflowStage.REVIEW
            repair_published = False

            def publish_repair(repaired_content: bytes) -> None:
                nonlocal final_sha, repair_published, stage
                if repair_published:
                    raise ValueError("T011 review invoked the repair callback more than once")
                repaired = WorkflowCandidate(repaired_content, candidate.review_context)
                stage = WorkflowStage.VALIDATE
                self._content.validate_candidate(snapshot, repaired)
                stage = WorkflowStage.PUBLISH
                final_sha = self._publisher.publish(snapshot, repaired)
                repair_published = True
                stage = WorkflowStage.REVIEW

            review = self._reviewer.review(
                snapshot,
                candidate,
                before_final_critic=publish_repair,
            )
            if review.repair_applied is not repair_published:
                raise ValueError("T011 review returned an inconsistent repair result")
            stage = WorkflowStage.REPORT
            self._reporter.update_current_pr(
                mode=mode,
                pr_number=request.pr_number,
                branch=snapshot.branch,
                commit_sha=final_sha,
                review=review,
            )
            result = WorkflowResult(
                job_id,
                mode,
                final_sha,
                review.final.verdict,
                review.repair_applied,
            )
        except Exception as error:  # noqa: BLE001 - ports may expose arbitrary safe boundaries.
            self._fail_job(job_id, mode, stage, error, audit_started_at, final_sha)
        self._succeed_job(job_id, mode, audit_started_at, result.final_commit_sha)
        return result

    def doc_verify(self, request: VerifyWorkflowInput, /) -> WorkflowResult:
        mode = Mode.DOC_VERIFY
        job_id, audit_started_at = self._start_job(
            mode,
            request.pr_number,
            request.source_sha,
            target_sha=request.target_sha,
        )
        final_sha: GitSha | None = request.target_sha
        stage = WorkflowStage.AUTHORIZE
        try:
            authorization = self._source.authorize_verify(request)
            self._require_mode(authorization.mode, mode)
            stage = WorkflowStage.SNAPSHOT
            snapshot = self._source.snapshot_verify(authorization)
            self._require_snapshot(snapshot, authorization, mode)
            stage = WorkflowStage.LOAD_CANDIDATE
            candidate = self._content.load_verification_candidate(snapshot)
            stage = WorkflowStage.VALIDATE
            self._content.validate_candidate(snapshot, candidate)
            stage = WorkflowStage.REVIEW
            final_sha = snapshot.target_sha
            if final_sha is None:
                raise ValueError("doc_verify snapshot has no target SHA")
            repair_published = False

            def publish_repair(repaired_content: bytes) -> None:
                nonlocal final_sha, repair_published, stage
                if repair_published:
                    raise ValueError("T011 review invoked the repair callback more than once")
                repaired = WorkflowCandidate(repaired_content, candidate.review_context)
                stage = WorkflowStage.VALIDATE
                self._content.validate_candidate(snapshot, repaired)
                stage = WorkflowStage.PUBLISH
                final_sha = self._publisher.publish(snapshot, repaired)
                repair_published = True
                stage = WorkflowStage.REVIEW

            review = self._reviewer.review(
                snapshot,
                candidate,
                before_final_critic=publish_repair,
            )
            if review.repair_applied is not repair_published:
                raise ValueError("T011 review returned an inconsistent repair result")
            stage = WorkflowStage.REPORT
            self._reporter.update_current_pr(
                mode=mode,
                pr_number=request.pr_number,
                branch=snapshot.branch,
                commit_sha=final_sha,
                review=review,
            )
            result = WorkflowResult(
                job_id,
                mode,
                final_sha,
                review.final.verdict,
                review.repair_applied,
            )
        except Exception as error:  # noqa: BLE001 - ports may expose arbitrary safe boundaries.
            self._fail_job(job_id, mode, stage, error, audit_started_at, final_sha)
        self._succeed_job(job_id, mode, audit_started_at, result.final_commit_sha)
        return result

    def _start_job(
        self,
        mode: Mode,
        pr_number: int,
        source_sha: GitSha,
        *,
        target_sha: GitSha | None,
    ) -> tuple[str, datetime]:
        try:
            started_at = self._clock.now()
            job_id = self._persistence.start_job(
                mode,
                pr_number=pr_number,
                source_sha=source_sha.value,
                target_sha=target_sha.value if target_sha is not None else None,
                started_at=started_at,
            )
            return job_id, started_at
        except Exception:  # noqa: BLE001 - sanitize every injected audit/clock failure.
            raise WorkflowError(mode, WorkflowStage.AUDIT_START) from None

    def _succeed_job(
        self, job_id: str, mode: Mode, audit_started_at: datetime, final_sha: GitSha
    ) -> None:
        try:
            finished_at = self._clock.now()
        except Exception:  # noqa: BLE001 - a failed clock is still terminalized.
            self._record_failed_terminal(
                job_id,
                mode,
                safe_error="terminal_audit_failed",
                fallback_time=audit_started_at,
                target_sha=final_sha,
            )
            raise WorkflowError(mode, WorkflowStage.TERMINAL_AUDIT) from None
        try:
            self._persistence.finish_job(
                job_id,
                JobStatus.SUCCEEDED,
                error=None,
                finished_at=finished_at,
                target_sha=final_sha.value,
            )
        except Exception:  # noqa: BLE001 - a terminal write must not expose its boundary error.
            self._record_failed_terminal(
                job_id,
                mode,
                safe_error="terminal_audit_failed",
                fallback_time=audit_started_at,
                target_sha=final_sha,
            )
            raise WorkflowError(mode, WorkflowStage.TERMINAL_AUDIT) from None

    def _fail_job(
        self,
        job_id: str,
        mode: Mode,
        stage: WorkflowStage,
        error: Exception,
        audit_started_at: datetime,
        target_sha: GitSha | None,
    ) -> NoReturn:
        is_quota = stage is WorkflowStage.BUDGET and isinstance(error, DailyBudgetExceeded)
        safe_error = DailyBudgetExceeded.user_message if is_quota else f"{stage.value}_failed"
        self._record_failed_terminal(
            job_id,
            mode,
            safe_error=safe_error,
            fallback_time=audit_started_at,
            target_sha=target_sha,
        )
        if is_quota:
            raise DailyBudgetExceeded from None
        raise WorkflowError(mode, stage) from None

    def _record_failed_terminal(
        self,
        job_id: str,
        mode: Mode,
        *,
        safe_error: str,
        fallback_time: datetime,
        target_sha: GitSha | None,
    ) -> None:
        try:
            self._persistence.finish_job(
                job_id,
                JobStatus.FAILED,
                error=safe_error,
                finished_at=self._terminal_time(fallback_time),
                target_sha=target_sha.value if target_sha is not None else None,
            )
        except Exception:  # noqa: BLE001 - failed-audit diagnostics may contain persisted values.
            raise WorkflowError(mode, WorkflowStage.TERMINAL_AUDIT) from None

    def _terminal_time(self, fallback_time: datetime) -> datetime:
        try:
            return self._clock.now()
        except Exception:  # noqa: BLE001 - the known job-start time permits the required write.
            return fallback_time

    @staticmethod
    def _require_mode(actual: Mode, expected: Mode) -> None:
        if actual is not expected:
            raise ValueError("workflow port returned the wrong mode")

    @staticmethod
    def _require_snapshot(
        snapshot: ImmutableRunSnapshot,
        authorization: AuthorizedRun,
        mode: Mode,
    ) -> None:
        if snapshot.mode is not mode or snapshot.branch != authorization.branch:
            raise ValueError("snapshot does not match authorization")
        if mode is Mode.DOC_VERIFY and snapshot.target_sha != authorization.current_target_sha:
            raise ValueError("verify snapshot does not match the authorized branch SHA")
