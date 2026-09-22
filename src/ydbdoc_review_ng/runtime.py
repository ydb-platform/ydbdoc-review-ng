"""Installable production composition. Construction is lazy and performs no I/O.

Tests replace only HTTP transports and the YDB executor, not workflow stages.
Each factory instance is one job; credentials are never included in diagnostics.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from ydbdoc_review_ng.application import (
    AuthorizedRun,
    ContinueWorkflowInput,
    ImmutableRunSnapshot,
    LinearWorkflows,
    TranslateWorkflowInput,
    VerifyWorkflowInput,
    WorkflowResult,
)
from ydbdoc_review_ng.continuation import SourceChangeInventory, normalize_source_inventory
from ydbdoc_review_ng.domain import GitSha, Mode, RepositoryId, SnapshotRef
from ydbdoc_review_ng.models import (
    AttemptResult,
    HttpTransport,
    ModelCallResult,
    ModelRequest,
    ModelTokenPrice,
    NativeYandexClient,
    PerModelPricing,
    UrllibTransport,
    YandexCredentials,
)
from ydbdoc_review_ng.persistence import ContinuationCheckpoint, YdbExecutor, YdbPersistence
from ydbdoc_review_ng.publication import GitPublicationAdapter, PublicationContext
from ydbdoc_review_ng.quality import QualityReviewResult
from ydbdoc_review_ng.reporting import QAReporter, ReportContext
from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
from ydbdoc_review_ng.runtime_continue import CheckpointReader, ContinueAdmission, admit_continue
from ydbdoc_review_ng.runtime_github import (
    GitHubBackend,
    GitHubHTTP,
    JsonTransport,
    RuntimeBoundaryError,
)
from ydbdoc_review_ng.runtime_ydb import DEFAULT_YDB_DATABASE, DEFAULT_YDB_ENDPOINT, SDKExecutor

_YANDEXGPT_5_1_TOKEN_RUB = Decimal("0.0012")
_PRODUCTION_PRICING = PerModelPricing(
    {
        "yandexgpt-5.1/latest": ModelTokenPrice(
            _YANDEXGPT_5_1_TOKEN_RUB,
            _YANDEXGPT_5_1_TOKEN_RUB,
            _YANDEXGPT_5_1_TOKEN_RUB,
        )
    }
)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RecordedModels:
    def __init__(
        self, environment: Mapping[str, str], persistence: YdbPersistence, transport: HttpTransport
    ) -> None:
        self.environment, self.persistence, self.transport = environment, persistence, transport
        self.cost: Decimal | None = Decimal(0)
        self.job_id: str | None = None

    def bind_job(self, job_id: str) -> None:
        if not job_id:
            raise RuntimeBoundaryError("model_job_missing")
        self.job_id = job_id
        self.cost = Decimal(0)

    def record(self, attempt: AttemptResult) -> None:
        if self.job_id is None:
            raise RuntimeBoundaryError("model_job_missing")
        self.persistence(attempt, job_id=self.job_id)
        self.cost = (
            None if self.cost is None or attempt.cost_rub is None else self.cost + attempt.cost_rub
        )

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        if self.job_id is None:
            raise RuntimeBoundaryError("model_job_missing")
        client = NativeYandexClient(
            YandexCredentials(
                self.environment.get("YANDEX_API_KEY", ""),
                self.environment.get("YANDEX_FOLDER_ID", ""),
            ),
            self.transport,
            self.record,
            pricing=_PRODUCTION_PRICING,
        )
        return client.invoke(request)


class RuntimeSource:
    def __init__(self, environment: Mapping[str, str], github: GitHubBackend) -> None:
        self.environment, self.github = environment, github
        self.snapshots: ResolvedRepositorySnapshots
        self.context: PublicationContext
        self.inventory = SourceChangeInventory(())
        self.metadata_snapshot: SnapshotRef
        self.source_pr = 0
        self.continue_target_sha: GitSha | None = None

    def _authorize(self) -> None:
        actor = self.environment.get("GITHUB_TRIGGERING_ACTOR") or self.environment.get(
            "GITHUB_ACTOR", ""
        )
        allowed = re.split(r"[,\s]+", self.environment.get("YDBDOC_ALLOWED_ACTORS", "").strip())
        if not actor or actor not in allowed:
            raise RuntimeBoundaryError("actor_not_authorized")

    def authorize_translate(self, request: TranslateWorkflowInput, /) -> AuthorizedRun:
        self._authorize()
        return AuthorizedRun(
            Mode.DOC_TRANSLATE, f"translation/pr-{request.pr_number}", None, request
        )

    def authorize_continue(
        self, pr_number: int, checkpoints: CheckpointReader, /, *, now: datetime
    ) -> ContinueAdmission:
        """Read-only admission using actual label/comment actors, not Actions actor env."""
        allowed = frozenset(
            actor
            for actor in re.split(
                r"[,\s]+", self.environment.get("YDBDOC_ALLOWED_ACTORS", "").strip()
            )
            if actor
        )
        return admit_continue(self.github, checkpoints, pr_number, allowed, now=now)

    def authorize_verify(self, request: VerifyWorkflowInput, /) -> AuthorizedRun:
        self._authorize()
        pr = self.github.request("GET", f"/pulls/{request.pr_number}")
        if (
            pr["head"]["repo"]["full_name"] != self.github.repository
            or pr["head"]["sha"] != request.target_sha.value
        ):
            raise RuntimeBoundaryError("verification_head_mismatch")
        match = re.search(r"<!-- ydbdoc-source-pr:(\d+) -->", pr.get("body") or "")
        if match is None:
            raise RuntimeBoundaryError("source_pr_provenance_missing")
        pinned = re.search(r"<!-- ydbdoc-source-sha:([0-9a-f]{40}) -->", pr.get("body") or "")
        if pinned is None or pinned[1] != request.source_sha.value:
            raise RuntimeBoundaryError("source_sha_provenance_mismatch")
        return AuthorizedRun(
            Mode.DOC_VERIFY,
            pr["head"]["ref"],
            request.target_sha,
            (request, int(match[1]), pr["base"]["ref"]),
        )

    def _snapshot(
        self,
        authorization: AuthorizedRun,
        source_pr: int,
        expected_source: GitSha,
        expected_base: str | None = None,
    ) -> ImmutableRunSnapshot:
        pr = self.github.request("GET", f"/pulls/{source_pr}")
        if pr["base"]["repo"]["full_name"] != self.github.repository:
            raise RuntimeBoundaryError("repository_mismatch")
        base = BaseBranch(pr["base"]["ref"])
        if expected_base is not None and base.value != expected_base:
            raise RuntimeBoundaryError("base_mismatch")
        tip = self.github.head(base.value)
        if tip is None:
            raise RuntimeBoundaryError("base_missing")
        repository = RepositoryId(self.github.repository)
        base_snapshot = SnapshotRef(repository, tip)
        merged = bool(pr["merged"])
        original = SnapshotRef(
            repository, GitSha(pr["merge_commit_sha"] if merged else pr["head"]["sha"])
        )
        source = SnapshotRef(repository, expected_source) if merged else original
        # Verify keeps the previously pinned authoritative source even if base advances.
        if authorization.mode is Mode.DOC_VERIFY:
            if not merged and original.commit_sha != expected_source:
                raise RuntimeBoundaryError("source_pr_changed")
            source = SnapshotRef(repository, expected_source)
        elif source.commit_sha != expected_source:
            raise RuntimeBoundaryError("source_sha_mismatch")
        self.snapshots = ResolvedRepositorySnapshots(
            PullRequestState.MERGED if merged else PullRequestState.OPEN,
            base,
            original if merged else source,
            source,
            source,
            source,
            source if merged else base_snapshot,
            source if merged else None,
            original if merged else None,
        )
        changes = self.github.request("GET", f"/pulls/{source_pr}/files?per_page=100")
        if len(changes) != pr["changed_files"]:
            raise RuntimeBoundaryError("source_change_list_incomplete")
        self.inventory = normalize_source_inventory(changes)
        # Reject source movement while resolving the diff inventory.
        fresh = self.github.request("GET", f"/pulls/{source_pr}")
        if fresh["head"]["sha"] != pr["head"]["sha"] or fresh["base"]["ref"] != base.value:
            raise RuntimeBoundaryError("source_pr_changed")
        head = self.github.head(authorization.branch)
        if (
            authorization.current_target_sha is not None
            and head != authorization.current_target_sha
        ):
            raise RuntimeBoundaryError("verification_head_mismatch")
        self.context = PublicationContext(
            self.github.repository,
            authorization.branch,
            base.value,
            base.value,
            head or (source.commit_sha if merged else tip),
        )
        self.metadata_snapshot = SnapshotRef(repository, self.context.current_head)
        self.source_pr = source_pr
        self.github.source_pr = source_pr
        self.github.source_sha = source.commit_sha
        return ImmutableRunSnapshot(
            authorization.mode,
            source.commit_sha,
            authorization.current_target_sha,
            authorization.branch,
            self.context,
        )

    def snapshot_translate(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot:
        request = authorization.context
        assert isinstance(request, TranslateWorkflowInput)
        return self._snapshot(authorization, request.pr_number, request.source_sha)

    def snapshot_verify(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot:
        request, source_pr, base = cast(tuple[VerifyWorkflowInput, int, str], authorization.context)
        return self._snapshot(authorization, source_pr, request.source_sha, base)

    def snapshot_continue(self, checkpoint: ContinuationCheckpoint, /) -> ImmutableRunSnapshot:
        """Restore scope inputs from saved refs/inventory, never today's PR file list.

        The scope reader needs only the normalized snapshot table. The old PR's
        merge commit is not a source or a diff baseline for replay.
        """
        pr = self.github.read_pull_request_identity(checkpoint.source_pr)
        if pr.base_repository != self.github.repository or pr.provenance is not None:
            raise RuntimeBoundaryError("continue_source_identity_mismatch")
        head = self.github.head(checkpoint.translation_branch)
        if head != checkpoint.target_sha:
            raise RuntimeBoundaryError("continue_translation_head_mismatch")
        self.continue_target_sha = checkpoint.target_sha
        repository = RepositoryId(self.github.repository)
        source = SnapshotRef(repository, checkpoint.source_sha)
        base = SnapshotRef(repository, checkpoint.base_sha)
        self.snapshots = ResolvedRepositorySnapshots(
            PullRequestState.OPEN,
            BaseBranch(pr.base_branch),
            source,
            source,
            source,
            source,
            base,
            None,
            None,
        )
        self.inventory = checkpoint.source_inventory
        # Current target is an identity/publication parent only. Metadata starts
        # at the saved base and source, including for a previously merged PR.
        self.metadata_snapshot = base
        self.context = PublicationContext(
            self.github.repository,
            checkpoint.translation_branch,
            pr.base_branch,
            pr.base_branch,
            checkpoint.target_sha or checkpoint.base_sha,
        )
        self.source_pr = checkpoint.source_pr
        self.github.source_pr = checkpoint.source_pr
        self.github.source_sha = checkpoint.source_sha
        return ImmutableRunSnapshot(
            Mode.DOC_CONTINUE,
            checkpoint.source_sha,
            checkpoint.target_sha,
            checkpoint.translation_branch,
            self.context,
        )


class RuntimeReporter:
    def __init__(
        self, source: RuntimeSource, publisher: GitPublicationAdapter, models: RecordedModels
    ) -> None:
        self.source, self.publisher, self.models = source, publisher, models

    def update_current_pr(
        self,
        *,
        mode: Mode,
        pr_number: int,
        branch: str,
        commit_sha: GitSha,
        review: QualityReviewResult,
    ) -> None:
        if mode is Mode.DOC_TRANSLATE and self.publisher.noop:
            return
        head = self.source.github.head(branch)
        if (
            mode is Mode.DOC_CONTINUE
            and self.publisher.noop
            and self.source.continue_target_sha is None
            and head is None
        ):
            return
        if head != commit_sha:
            raise RuntimeBoundaryError("report_head_changed")
        reporter = QAReporter(
            self.source.github,
            self.publisher,
            lambda: ReportContext(
                self.source.snapshots.source_snapshot.commit_sha, commit_sha, self.models.cost
            ),
            lambda: self.source.github.checks(commit_sha),
            verification_context=self.source.context,
            current_head=(
                (lambda: self.source.github.head(branch)) if mode is Mode.DOC_CONTINUE else None
            ),
        )
        reporter.update_current_pr(
            mode=mode, pr_number=pr_number, branch=branch, commit_sha=commit_sha, review=review
        )


class Runtime:
    """One workflow dispatcher with deterministic shutdown for owned resources."""

    __slots__ = ("_shutdown", "_shutdown_complete", "_workflows")

    def __init__(
        self, workflows: LinearWorkflows, shutdown: Callable[[], None] | None = None, /
    ) -> None:
        self._workflows = workflows
        self._shutdown = shutdown
        self._shutdown_complete = False

    def doc_translate(self, request: TranslateWorkflowInput, /) -> WorkflowResult:
        return self._workflows.doc_translate(request)

    def doc_verify(self, request: VerifyWorkflowInput, /) -> WorkflowResult:
        return self._workflows.doc_verify(request)

    def doc_continue(self, request: ContinueWorkflowInput, /) -> WorkflowResult:
        return self._workflows.doc_continue(request)

    def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        if self._shutdown is not None:
            self._shutdown()


def create_runtime(
    *,
    environment: Mapping[str, str] | None = None,
    ydb_executor: YdbExecutor | None = None,
    github_transport: JsonTransport | None = None,
    model_transport: HttpTransport | None = None,
) -> Runtime:
    """One job's real composition; optional arguments replace only external I/O."""
    from ydbdoc_review_ng.runtime_content import RuntimeContent

    env = dict(os.environ if environment is None else environment)
    shutdown = None
    if ydb_executor is None:
        owned_executor = SDKExecutor(
            env.get("YDB_ENDPOINT") or env.get("YDBDOC_YDB_ENDPOINT") or DEFAULT_YDB_ENDPOINT,
            env.get("YDB_DATABASE") or env.get("YDBDOC_YDB_DATABASE") or DEFAULT_YDB_DATABASE,
            env.get("YDB_TOKEN", ""),
            env.get("YDB_SA_KEY", ""),
        )
        executor: YdbExecutor = owned_executor
        shutdown = owned_executor.close
    else:
        executor = ydb_executor
    persistence = YdbPersistence(executor)
    github = GitHubBackend(
        github_transport
        or GitHubHTTP(
            env.get("YDBDOC_GITHUB_READ_TOKEN", ""),
            env.get("YDB_GH_TOKEN") or env.get("GH_TOKEN", ""),
        )
    )
    models = RecordedModels(env, persistence, model_transport or UrllibTransport())
    source = RuntimeSource(env, github)
    content = RuntimeContent(source, models, env)
    publisher = GitPublicationAdapter(github, content.publication_plan, content.validate_plan)
    content.publisher = publisher
    return Runtime(
        LinearWorkflows(
            clock=SystemClock(),
            persistence=persistence,
            source=source,
            content=content,
            reviewer=content,
            publisher=publisher,
            reporter=RuntimeReporter(source, publisher, models),
            bind_models=models.bind_job,
        ),
        shutdown,
    )
