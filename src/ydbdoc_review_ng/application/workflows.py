"""Straight-line orchestration over already implemented component boundaries."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, NoReturn, Protocol

from ydbdoc_review_ng.continuation import (
    ContinuationState,
    SourceChangeInventory,
)
from ydbdoc_review_ng.domain import GitSha, Mode, RepoPath
from ydbdoc_review_ng.errors import SafeDiagnosticError
from ydbdoc_review_ng.persistence import (
    ContinuationCheckpoint,
    DailyBudgetExceeded,
    JobStatus,
    semantic_stop_error,
)
from ydbdoc_review_ng.ports import Clock
from ydbdoc_review_ng.quality import QualityReviewResult, Verdict
from ydbdoc_review_ng.trace import write_trace

if TYPE_CHECKING:
    from ydbdoc_review_ng.runtime_continue import (
        CheckpointReader,
        ContinueAdmission,
        ContinueReplay,
    )


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
    CHECKPOINT = "checkpoint"
    TERMINAL_AUDIT = "terminal_audit"


class WorkflowError(RuntimeError):
    """A workflow failure retaining only stage and an optional safe code."""

    def __init__(
        self,
        mode: Mode,
        stage: WorkflowStage,
        diagnostic: SafeDiagnosticError | None = None,
        /,
    ) -> None:
        self.mode = mode
        self.stage = stage
        self.diagnostic = None if diagnostic is None else diagnostic.code
        message = f"{mode.value} workflow failed during {stage.value}"
        if self.diagnostic is not None:
            message += f": {self.diagnostic}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CheckpointCapture:
    source_pr: int
    source_sha: GitSha
    base_sha: GitSha
    translation_branch: str
    target_sha: GitSha | None
    source_inventory: SourceChangeInventory
    scope_target_paths: tuple[RepoPath, ...]
    state: ContinuationState = field(repr=False)
    translation_pr: int | None = None

    def record(self, job_id: str, trigger_pr: int, created_at: datetime) -> ContinuationCheckpoint:
        return ContinuationCheckpoint(
            job_id,
            job_id,
            self.source_pr,
            self.translation_pr or trigger_pr,
            self.source_sha,
            self.base_sha,
            self.translation_branch,
            self.target_sha,
            self.source_inventory,
            self.scope_target_paths,
            self.state,
            created_at,
        )


class SemanticCheckpointStop(RuntimeError):
    """A supported semantic boundary with a validated continuation capture."""

    def __init__(self, capture: CheckpointCapture) -> None:
        self.capture = capture
        super().__init__(f"semantic_stop:{capture.state.stage.value}")


def _require_pr_number(value: object, type_name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{type_name}.pr_number must be a positive integer")


@dataclass(frozen=True, slots=True)
class ContinueWorkflowInput:
    pr_number: int

    def __post_init__(self) -> None:
        _require_pr_number(self.pr_number, type(self).__name__)


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
        if self.mode not in {Mode.DOC_TRANSLATE, Mode.DOC_VERIFY, Mode.DOC_CONTINUE}:
            raise ValueError("ImmutableRunSnapshot.mode must be a supported workflow mode")
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
        source_sha: str | None,
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

    def bind_job_snapshot(
        self, job_id: str, /, *, source_sha: str, target_sha: str | None
    ) -> None: ...

    def save_checkpoint(
        self, checkpoint: ContinuationCheckpoint, /, *, now: datetime
    ) -> ContinuationCheckpoint: ...

    def activate_checkpoint(
        self, checkpoint: ContinuationCheckpoint, /, *, now: datetime
    ) -> ContinuationCheckpoint: ...

    def validate_checkpoint_job(self, checkpoint: ContinuationCheckpoint, /) -> None: ...

    def load_checkpoint(
        self,
        pr_number: int,
        /,
        *,
        now: datetime,
        source_sha: GitSha | None = None,
        target_sha: GitSha | None = None,
    ) -> ContinuationCheckpoint: ...

    def close_checkpoint(self, continuation_id: str, /) -> None: ...

    def consume_checkpoint(
        self, checkpoint: ContinuationCheckpoint, job_id: str, /, *, now: datetime
    ) -> None: ...

    def finish_job_reconciled(
        self,
        job_id: str,
        status: JobStatus,
        /,
        *,
        error: str | None,
        finished_at: datetime,
        target_sha: str | None,
    ) -> None: ...


class SourceWorkflowPort(Protocol):
    def authorize_continue(
        self, pr_number: int, checkpoints: CheckpointReader, /, *, now: datetime
    ) -> ContinueAdmission: ...

    def authorize_translate(self, request: TranslateWorkflowInput, /) -> AuthorizedRun: ...

    def snapshot_translate(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot: ...

    def authorize_verify(self, request: VerifyWorkflowInput, /) -> AuthorizedRun: ...

    def snapshot_verify(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot: ...


class ContentWorkflowPort(Protocol):
    def replay_continuation(self, checkpoint: ContinuationCheckpoint, /) -> ContinueReplay: ...

    def prepare_continuation(
        self,
        replay: ContinueReplay,
        checkpoint: ContinuationCheckpoint,
        /,
        *,
        operator_context: str,
    ) -> WorkflowCandidate: ...

    def prepare_translation(self, snapshot: ImmutableRunSnapshot, /) -> WorkflowCandidate: ...

    def load_verification_candidate(
        self, snapshot: ImmutableRunSnapshot, /
    ) -> WorkflowCandidate: ...

    def validate_candidate(
        self, snapshot: ImmutableRunSnapshot, candidate: WorkflowCandidate, /
    ) -> None: ...

    def review_checkpoint(
        self, snapshot: ImmutableRunSnapshot, review: QualityReviewResult, target_sha: GitSha, /
    ) -> CheckpointCapture: ...


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
    """Execute translation, verification and semantic continuation linearly."""

    __slots__ = (
        "_bind_models",
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
        bind_models: Callable[[str], None] | None = None,
    ) -> None:
        self._clock = clock
        self._persistence = persistence
        self._source = source
        self._content = content
        self._reviewer = reviewer
        self._publisher = publisher
        self._reporter = reporter
        self._bind_models = bind_models

    def doc_continue(self, request: ContinueWorkflowInput, /) -> WorkflowResult:
        mode = Mode.DOC_CONTINUE
        job_id, started_at = self._start_job(mode, request.pr_number, None, target_sha=None)
        final_sha: GitSha | None = None
        checkpoint: ContinuationCheckpoint | None = None
        terminal_handoff = False
        stage = WorkflowStage.AUTHORIZE
        try:
            admission = self._source.authorize_continue(
                request.pr_number, self._persistence, now=self._clock.now()
            )
            checkpoint = admission.checkpoint
            self._persistence.validate_checkpoint_job(checkpoint)
            stage = WorkflowStage.SNAPSHOT
            replay = self._content.replay_continuation(checkpoint)
            snapshot = replay.preparation.snapshot
            if (
                snapshot.mode is not mode
                or snapshot.source_sha != checkpoint.source_sha
                or snapshot.target_sha != checkpoint.target_sha
                or snapshot.branch != checkpoint.translation_branch
            ):
                raise ValueError("continuation snapshot mismatch")
            final_sha = snapshot.target_sha
            self._persistence.bind_job_snapshot(
                job_id,
                source_sha=snapshot.source_sha.value,
                target_sha=None if final_sha is None else final_sha.value,
            )
            stage = WorkflowStage.PREPARE
            candidate = self._content.prepare_continuation(
                replay, checkpoint, operator_context=admission.trigger.operator_context
            )
            stage = WorkflowStage.VALIDATE
            self._content.validate_candidate(snapshot, candidate)
            stage = WorkflowStage.PUBLISH
            final_sha = self._publisher.publish(snapshot, candidate)
            published_content = candidate.content
            repair_published = False
            stage = WorkflowStage.REVIEW

            def publish_repair(repaired_content: bytes) -> None:
                nonlocal final_sha, repair_published, stage, published_content
                if repair_published:
                    raise ValueError("T011 review invoked the repair callback more than once")
                repaired = WorkflowCandidate(repaired_content, candidate.review_context)
                stage = WorkflowStage.VALIDATE
                self._content.validate_candidate(snapshot, repaired)
                stage = WorkflowStage.PUBLISH
                final_sha = self._publisher.publish(snapshot, repaired)
                published_content = repaired_content
                repair_published = True
                stage = WorkflowStage.REVIEW

            review = self._reviewer.review(snapshot, candidate, before_final_critic=publish_repair)
            if review.repair_applied is not repair_published:
                raise ValueError("T011 review returned an inconsistent repair result")
            if review.final_candidate != published_content:
                raise ValueError("review candidate differs from the published candidate")
            stage = WorkflowStage.REPORT
            self._reporter.update_current_pr(
                mode=mode,
                pr_number=request.pr_number,
                branch=snapshot.branch,
                commit_sha=final_sha,
                review=review,
            )
            if review.final.verdict is Verdict.RED:
                stage = WorkflowStage.CHECKPOINT
                capture = self._content.review_checkpoint(snapshot, review, final_sha)
                terminal_handoff = True
                self._complete_semantic_handoff(
                    capture,
                    job_id,
                    request.pr_number,
                    mode,
                    started_at,
                    previous=checkpoint,
                )
            else:
                stage = WorkflowStage.CHECKPOINT
                self._persistence.consume_checkpoint(checkpoint, job_id, now=self._clock.now())
                stage = WorkflowStage.TERMINAL_AUDIT
                finished_at = self._clock.now()
                terminal_handoff = True
                self._persistence.finish_job_reconciled(
                    job_id,
                    JobStatus.SUCCEEDED,
                    error=None,
                    finished_at=finished_at,
                    target_sha=final_sha.value,
                )
                with suppress(Exception):
                    self._persistence.close_checkpoint(checkpoint.continuation_id)
            return WorkflowResult(job_id, mode, final_sha, review.final.verdict, repair_published)
        except SemanticCheckpointStop as stop:
            assert checkpoint is not None
            self._complete_semantic_handoff(
                stop.capture,
                job_id,
                request.pr_number,
                mode,
                started_at,
                previous=checkpoint,
            )
            raise WorkflowError(mode, stage) from None
        except Exception as error:  # noqa: BLE001 - ports may expose arbitrary safe boundaries.
            if terminal_handoff:
                # Its durable marker is authoritative even when acknowledgement
                # or read-back is unavailable. Never overwrite a possible SUCCESS.
                raise WorkflowError(mode, stage) from None
            self._fail_job(job_id, mode, stage, error, started_at, final_sha)

    def doc_translate(self, request: TranslateWorkflowInput, /) -> WorkflowResult:
        mode = Mode.DOC_TRANSLATE
        job_id, audit_started_at = self._start_job(
            mode,
            request.pr_number,
            request.source_sha,
            target_sha=None,
        )
        final_sha: GitSha | None = None
        semantic_terminal = False
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
            published_content = candidate.content
            stage = WorkflowStage.REVIEW
            repair_published = False

            def publish_repair(repaired_content: bytes) -> None:
                nonlocal final_sha, repair_published, stage, published_content
                if repair_published:
                    raise ValueError("T011 review invoked the repair callback more than once")
                repaired = WorkflowCandidate(repaired_content, candidate.review_context)
                stage = WorkflowStage.VALIDATE
                self._content.validate_candidate(snapshot, repaired)
                stage = WorkflowStage.PUBLISH
                final_sha = self._publisher.publish(snapshot, repaired)
                published_content = repaired_content
                repair_published = True
                stage = WorkflowStage.REVIEW

            review = self._reviewer.review(
                snapshot,
                candidate,
                before_final_critic=publish_repair,
            )
            if review.repair_applied is not repair_published:
                raise ValueError("T011 review returned an inconsistent repair result")
            if review.final_candidate != published_content:
                raise ValueError("review candidate differs from the published candidate")
            stage = WorkflowStage.REPORT
            self._reporter.update_current_pr(
                mode=mode,
                pr_number=request.pr_number,
                branch=snapshot.branch,
                commit_sha=final_sha,
                review=review,
            )
            if review.final.verdict is Verdict.RED and review.accepted_maps is not None:
                stage = WorkflowStage.CHECKPOINT
                capture = self._content.review_checkpoint(snapshot, review, final_sha)
                self._complete_semantic_handoff(
                    capture, job_id, request.pr_number, mode, audit_started_at
                )
                semantic_terminal = True
            result = WorkflowResult(
                job_id,
                mode,
                final_sha,
                review.final.verdict,
                review.repair_applied,
            )
        except SemanticCheckpointStop as stop:
            self._semantic_stop(stop, job_id, request.pr_number, mode, stage, audit_started_at)
        except Exception as error:  # noqa: BLE001 - ports may expose arbitrary safe boundaries.
            self._fail_job(job_id, mode, stage, error, audit_started_at, final_sha)
        if not semantic_terminal:
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
        semantic_terminal = False
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
            published_content = candidate.content
            repair_published = False

            def publish_repair(repaired_content: bytes) -> None:
                nonlocal final_sha, repair_published, stage, published_content
                if repair_published:
                    raise ValueError("T011 review invoked the repair callback more than once")
                repaired = WorkflowCandidate(repaired_content, candidate.review_context)
                stage = WorkflowStage.VALIDATE
                self._content.validate_candidate(snapshot, repaired)
                stage = WorkflowStage.PUBLISH
                final_sha = self._publisher.publish(snapshot, repaired)
                published_content = repaired_content
                repair_published = True
                stage = WorkflowStage.REVIEW

            review = self._reviewer.review(
                snapshot,
                candidate,
                before_final_critic=publish_repair,
            )
            if review.repair_applied is not repair_published:
                raise ValueError("T011 review returned an inconsistent repair result")
            if review.final_candidate != published_content:
                raise ValueError("review candidate differs from the published candidate")
            stage = WorkflowStage.REPORT
            self._reporter.update_current_pr(
                mode=mode,
                pr_number=request.pr_number,
                branch=snapshot.branch,
                commit_sha=final_sha,
                review=review,
            )
            if review.final.verdict is Verdict.RED and review.accepted_maps is not None:
                stage = WorkflowStage.CHECKPOINT
                capture = self._content.review_checkpoint(snapshot, review, final_sha)
                self._complete_semantic_handoff(
                    capture, job_id, request.pr_number, mode, audit_started_at
                )
                semantic_terminal = True
            result = WorkflowResult(
                job_id,
                mode,
                final_sha,
                review.final.verdict,
                review.repair_applied,
            )
        except SemanticCheckpointStop as stop:
            self._semantic_stop(stop, job_id, request.pr_number, mode, stage, audit_started_at)
        except Exception as error:  # noqa: BLE001 - ports may expose arbitrary safe boundaries.
            self._fail_job(job_id, mode, stage, error, audit_started_at, final_sha)
        if not semantic_terminal:
            self._succeed_job(job_id, mode, audit_started_at, result.final_commit_sha)
        return result

    def _complete_semantic_handoff(
        self,
        capture: CheckpointCapture,
        job_id: str,
        trigger_pr: int,
        mode: Mode,
        created_at: datetime,
        *,
        previous: ContinuationCheckpoint | None = None,
    ) -> None:
        checkpoint = capture.record(
            job_id, trigger_pr, created_at if previous is None else previous.created_at
        )
        if previous is not None:
            self._replace_semantic_handoff(checkpoint, previous, mode, created_at)
            return
        stage = WorkflowStage.CHECKPOINT
        try:
            pending = self._persistence.save_checkpoint(checkpoint, now=self._clock.now())
            stage = WorkflowStage.TERMINAL_AUDIT
            self._persistence.finish_job(
                job_id,
                JobStatus.FAILED,
                error=semantic_stop_error(capture.state.stage),
                finished_at=self._clock.now(),
                target_sha=None if capture.target_sha is None else capture.target_sha.value,
            )
            stage = WorkflowStage.CHECKPOINT
            self._persistence.activate_checkpoint(pending, now=self._clock.now())
        except Exception:  # noqa: BLE001 - each cleanup remains independent of acknowledgement loss.
            self._abandon_capture(checkpoint, mode, stage, created_at)
            raise WorkflowError(mode, stage) from None

    def _replace_semantic_handoff(
        self,
        checkpoint: ContinuationCheckpoint,
        previous: ContinuationCheckpoint,
        mode: Mode,
        started_at: datetime,
    ) -> None:
        stage = WorkflowStage.CHECKPOINT
        terminal_attempted = False
        try:
            pending = self._persistence.save_checkpoint(checkpoint, now=self._clock.now())
            stage = WorkflowStage.TERMINAL_AUDIT
            finished_at = self._clock.now()
            terminal_attempted = True
            self._persistence.finish_job_reconciled(
                checkpoint.job_id,
                JobStatus.FAILED,
                error=semantic_stop_error(checkpoint.state.stage),
                finished_at=finished_at,
                target_sha=None if checkpoint.target_sha is None else checkpoint.target_sha.value,
            )
            stage = WorkflowStage.CHECKPOINT
            self._persistence.consume_checkpoint(previous, checkpoint.job_id, now=self._clock.now())
            self._persistence.activate_checkpoint(pending, now=self._clock.now())
        except Exception:  # noqa: BLE001 - pending/possibly activated rows must survive ambiguity.
            if not terminal_attempted:
                self._record_failed_terminal(
                    checkpoint.job_id,
                    mode,
                    safe_error=f"{stage.value}_failed",
                    fallback_time=started_at,
                    target_sha=checkpoint.target_sha,
                )
            raise WorkflowError(mode, stage) from None
        with suppress(Exception):
            self._persistence.close_checkpoint(previous.continuation_id)

    def _abandon_capture(
        self,
        checkpoint: ContinuationCheckpoint,
        mode: Mode,
        stage: WorkflowStage,
        fallback_time: datetime,
    ) -> None:
        # Neither cleanup failure may prevent the other write or expose payloads.
        with suppress(Exception):
            self._persistence.close_checkpoint(checkpoint.continuation_id)
        with suppress(Exception):
            self._record_failed_terminal(
                checkpoint.job_id,
                mode,
                safe_error=f"{stage.value}_failed",
                fallback_time=fallback_time,
                target_sha=checkpoint.target_sha,
            )

    def _semantic_stop(
        self,
        stop: SemanticCheckpointStop,
        job_id: str,
        trigger_pr: int,
        mode: Mode,
        stage: WorkflowStage,
        started_at: datetime,
    ) -> NoReturn:
        self._complete_semantic_handoff(stop.capture, job_id, trigger_pr, mode, started_at)
        raise WorkflowError(mode, stage) from None

    def _start_job(
        self,
        mode: Mode,
        pr_number: int,
        source_sha: GitSha | None,
        *,
        target_sha: GitSha | None,
    ) -> tuple[str, datetime]:
        try:
            started_at = self._clock.now()
            job_id = self._persistence.start_job(
                mode,
                pr_number=pr_number,
                source_sha=source_sha.value if source_sha is not None else None,
                target_sha=target_sha.value if target_sha is not None else None,
                started_at=started_at,
            )
        except Exception:  # noqa: BLE001 - sanitize every injected audit/clock failure.
            raise WorkflowError(mode, WorkflowStage.AUDIT_START) from None
        try:
            if self._bind_models is not None:
                self._bind_models(job_id)
        except Exception as error:  # noqa: BLE001 - terminalize after successful audit creation.
            self._fail_job(job_id, mode, WorkflowStage.AUDIT_START, error, started_at, target_sha)
        return job_id, started_at

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
        diagnostic = error if isinstance(error, SafeDiagnosticError) else None
        write_trace(
            "workflow",
            "failure",
            "fail",
            job_id=job_id,
            mode=mode.value,
            stage=stage.value,
            code=diagnostic.code if diagnostic is not None else "unexpected_exception",
            error_type=type(error).__name__,
        )
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
        raise WorkflowError(mode, stage, diagnostic) from None

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
