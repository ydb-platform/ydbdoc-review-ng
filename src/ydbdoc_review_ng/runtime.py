"""Installable production composition. Construction is lazy and performs no I/O.

Tests replace only HTTP transports and the YDB executor, not workflow stages.
Each factory instance is one job; credentials are never included in diagnostics.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
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
from ydbdoc_review_ng.direction import Direction, InventoryFile
from ydbdoc_review_ng.domain import GitSha, Mode, ModelRole, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.locales import LocaleRoots
from ydbdoc_review_ng.models import (
    AttemptResult,
    ExecutionConfig,
    HttpTransport,
    ModelCallResult,
    ModelRequest,
    ModelTokenPrice,
    NativeYandexClient,
    PerModelPricing,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.persistence import ContinuationCheckpoint, YdbExecutor, YdbPersistence
from ydbdoc_review_ng.publication import (
    GitPublicationAdapter,
    PublicationContext,
)
from ydbdoc_review_ng.quality import QualityReviewResult, Verdict
from ydbdoc_review_ng.reporting import ProbableDuplicate, QAReporter, ReportContext, QA_MARKER, render_report
from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
from ydbdoc_review_ng.runtime_continue import CheckpointReader, ContinueAdmission, admit_continue
from ydbdoc_review_ng.runtime_github import (
    GitHubBackend,
    GitHubHTTP,
    JsonTransport,
    RuntimeBoundaryError,
)
from ydbdoc_review_ng.runtime_ydb import DEFAULT_YDB_DATABASE, DEFAULT_YDB_ENDPOINT, SDKExecutor
from ydbdoc_review_ng.trace import traced, write_trace

_YANDEXGPT_5_1_TOKEN_RUB = Decimal("0.0012")
_DEEPSEEK_V4_INPUT_TOKEN_RUB = Decimal("0.0003")
_DEEPSEEK_V4_CACHED_INPUT_TOKEN_RUB = Decimal("0.000075")
_DEEPSEEK_V4_OUTPUT_TOKEN_RUB = Decimal("0.0005")
_PRODUCTION_PRICING = PerModelPricing(
    {
        "yandexgpt-5.1": ModelTokenPrice(
            _YANDEXGPT_5_1_TOKEN_RUB,
            _YANDEXGPT_5_1_TOKEN_RUB,
            _YANDEXGPT_5_1_TOKEN_RUB,
        ),
        "yandexgpt-5.1/latest": ModelTokenPrice(
            _YANDEXGPT_5_1_TOKEN_RUB,
            _YANDEXGPT_5_1_TOKEN_RUB,
            _YANDEXGPT_5_1_TOKEN_RUB,
        ),
        "deepseek-v4-flash": ModelTokenPrice(
            _DEEPSEEK_V4_INPUT_TOKEN_RUB,
            _DEEPSEEK_V4_OUTPUT_TOKEN_RUB,
            Decimal(0),
            _DEEPSEEK_V4_CACHED_INPUT_TOKEN_RUB,
        ),
        "deepseek-v4-flash/latest": ModelTokenPrice(
            _DEEPSEEK_V4_INPUT_TOKEN_RUB,
            _DEEPSEEK_V4_OUTPUT_TOKEN_RUB,
            Decimal(0),
            _DEEPSEEK_V4_CACHED_INPUT_TOKEN_RUB,
        ),
    }
)

# Full-file critic/arbiter calls need well above the old 180s wall
# (Actions run 37009373894: TRANSPORT @ ~182s, http_status=null).
_DEFAULT_MODEL_HTTP_TIMEOUT_SECONDS = 600.0


def _model_http_timeout_seconds(environment: Mapping[str, str]) -> float:
    raw = environment.get("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_MODEL_HTTP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeBoundaryError("model_http_timeout_invalid") from error
    if not (value > 0):
        raise RuntimeBoundaryError("model_http_timeout_invalid")
    return value


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

    def prepare_request(self, request: ModelRequest, /):
        """Expose wire budget so critic/arbiter packing can split whole pairs (§4)."""
        client_type = (
            YandexOpenAIClient
            if "deepseek" in request.model.lower()
            else NativeYandexClient
        )
        client = client_type(
            YandexCredentials(
                self.environment.get("YANDEX_API_KEY", ""),
                self.environment.get("YANDEX_FOLDER_ID", ""),
            ),
            self.transport,
            self.record,
            pricing=_PRODUCTION_PRICING,
            execution=ExecutionConfig(max_attempts=1 if request.role is ModelRole.DIRECTION else 2),
            timeout_seconds=_model_http_timeout_seconds(self.environment),
        )
        return client.prepare_request(request)

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        if self.job_id is None:
            raise RuntimeBoundaryError("model_job_missing")
        client_type = (
            YandexOpenAIClient
            if "deepseek" in request.model.lower()
            else NativeYandexClient
        )
        client = client_type(
            YandexCredentials(
                self.environment.get("YANDEX_API_KEY", ""),
                self.environment.get("YANDEX_FOLDER_ID", ""),
            ),
            self.transport,
            self.record,
            pricing=_PRODUCTION_PRICING,
            execution=ExecutionConfig(max_attempts=1 if request.role is ModelRole.DIRECTION else 2),
            timeout_seconds=_model_http_timeout_seconds(self.environment),
        )
        details: dict[str, object] = {
            "model_role": request.role.value,
            "article": None if request.target_path is None else request.target_path.value,
        }
        with traced("model", "invoke", **details):
            result = client.invoke(request)
        write_trace(
            "model",
            "result",
            "ok"
            if result.success and (result.text is not None or result.tool_calls)
            else "fail",
            **details,
            attempts_total=len(result.attempts),
            code=None if result.failure is None else result.failure.value,
            http_status=None if not result.attempts else result.attempts[-1].http_status,
            response_status=(
                None if not result.attempts else result.attempts[-1].response_status
            ),
        )
        return result


class RuntimeSource:
    def __init__(self, environment: Mapping[str, str], github: GitHubBackend) -> None:
        self.environment, self.github = environment, github
        self.snapshots: ResolvedRepositorySnapshots
        self.context: PublicationContext
        self.inventory = SourceChangeInventory(())
        self.metadata_snapshot: SnapshotRef
        self.source_base_snapshot: SnapshotRef
        self.source_change_snapshot: SnapshotRef
        self.source_pr = 0
        self.continue_target_sha: GitSha | None = None
        self.probable_duplicates: tuple[ProbableDuplicate, ...] = ()
        self.verification_direction: Direction | None = None
        self.verification_target_paths: tuple[RepoPath, ...] = ()

    def classifier_files(self, roots: LocaleRoots) -> tuple[InventoryFile, ...]:
        """Read full PR versions from its fixed base/head, including every resource."""
        ru_root, en_root = (root.value.removesuffix("/core") for root in (roots.ru, roots.en))
        files = []
        for change in self.inventory.files:
            before = (
                None
                if change.old_path is None
                else self.github.read_bytes(self.source_base_snapshot, change.old_path)
            )
            after = (
                None
                if change.new_path is None
                else self.github.read_bytes(self.source_change_snapshot, change.new_path)
            )
            if (
                change.old_path is not None
                and before is None
                or change.new_path is not None
                and after is None
            ):
                raise RuntimeBoundaryError("source_inventory_version_missing")
            ru_to_en = en_to_ru = None
            if change.path.value.startswith(ru_root + "/"):
                ru_to_en = RepoPath(en_root + change.path.value[len(ru_root) :])
            elif change.path.value.startswith(en_root + "/"):
                en_to_ru = RepoPath(ru_root + change.path.value[len(en_root) :])
            files.append(
                InventoryFile(
                    change,
                    before,
                    after,
                    ru_to_en,
                    en_to_ru,
                )
            )
        return tuple(files)

    def _authorize(self) -> None:
        actor = self.environment.get("GITHUB_TRIGGERING_ACTOR") or self.environment.get(
            "GITHUB_ACTOR", ""
        )
        allowed = re.split(r"[,\s]+", self.environment.get("YDBDOC_ALLOWED_ACTORS", "").strip())
        if not actor or actor not in allowed:
            raise RuntimeBoundaryError("actor_not_authorized")

    def _pr_files(self, number: int, expected: int) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        page = 1
        while not files or len(files) < expected:
            suffix = "" if page == 1 else f"&page={page}"
            batch = self.github.request("GET", f"/pulls/{number}/files?per_page=100{suffix}")
            if not batch:
                break
            files.extend(batch)
            page += 1
        if len(files) != expected:
            raise RuntimeBoundaryError("source_change_list_incomplete")
        return files

    def authorize_translate(self, request: TranslateWorkflowInput, /) -> AuthorizedRun:
        self._authorize()
        self.github.remove_label(request.pr_number, Mode.DOC_TRANSLATE.value)
        return AuthorizedRun(
            Mode.DOC_TRANSLATE, f"translation/pr-{request.pr_number}", None, request
        )

    def authorize_continue(
        self, pr_number: int, checkpoints: CheckpointReader, /, *, now: datetime
    ) -> ContinueAdmission:
        """Admission using actual label/comment actors, not Actions actor env."""
        allowed = frozenset(
            actor
            for actor in re.split(
                r"[,\s]+", self.environment.get("YDBDOC_ALLOWED_ACTORS", "").strip()
            )
            if actor
        )
        admission = admit_continue(self.github, checkpoints, pr_number, allowed, now=now)
        self.github.remove_label(pr_number, Mode.DOC_CONTINUE.value)
        return admission

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
        self.github.remove_label(request.pr_number, Mode.DOC_VERIFY.value)
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
        try:
            source_base_snapshot = SnapshotRef(repository, GitSha(pr["base"]["sha"]))
        except (KeyError, TypeError, ValueError):
            raise RuntimeBoundaryError("source_base_missing") from None
        merged = bool(pr["merged"])
        original = SnapshotRef(
            repository, GitSha(pr["merge_commit_sha"] if merged else pr["head"]["sha"])
        )
        change_snapshot = SnapshotRef(repository, GitSha(pr["head"]["sha"]))
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
        changes = self._pr_files(source_pr, pr["changed_files"])
        self.inventory = replace(
            normalize_source_inventory(changes),
            source_base_sha=source_base_snapshot.commit_sha,
            source_head_sha=change_snapshot.commit_sha,
        )
        # Reject source movement while resolving the diff inventory.
        fresh = self.github.request("GET", f"/pulls/{source_pr}")
        if (
            fresh["head"]["sha"] != pr["head"]["sha"]
            or fresh["base"]["ref"] != base.value
            or fresh["base"].get("sha") != source_base_snapshot.commit_sha.value
        ):
            raise RuntimeBoundaryError("source_pr_changed")
        head = self.github.head(authorization.branch)
        if (
            authorization.current_target_sha is not None
            and head != authorization.current_target_sha
        ):
            raise RuntimeBoundaryError("verification_head_mismatch")
        publication_parent = (
            self.snapshots.translation_base_snapshot.commit_sha
            if authorization.mode is Mode.DOC_TRANSLATE
            else head
        )
        if publication_parent is None:
            raise RuntimeBoundaryError("translation_head_missing")
        self.context = PublicationContext(
            self.github.repository,
            authorization.branch,
            base.value,
            base.value,
            publication_parent,
            expected_branch_head=head,
        )
        self.metadata_snapshot = SnapshotRef(repository, self.context.current_head)
        self.source_base_snapshot = source_base_snapshot
        self.source_change_snapshot = change_snapshot
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
        # §5.1: delete the previous remote translation branch before resolving head.
        self.github.delete_branch(authorization.branch)
        return self._snapshot(authorization, request.pr_number, request.source_sha)

    def snapshot_verify(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot:
        request, source_pr, base = cast(tuple[VerifyWorkflowInput, int, str], authorization.context)
        snapshot = self._snapshot(authorization, source_pr, request.source_sha, base)
        pr = self.github.request("GET", f"/pulls/{request.pr_number}")
        files = self._pr_files(request.pr_number, pr["changed_files"])
        target_inventory = normalize_source_inventory(files)
        fresh = self.github.request("GET", f"/pulls/{request.pr_number}")
        if (
            pr["head"]["sha"] != request.target_sha.value
            or fresh["head"]["sha"] != request.target_sha.value
            or fresh["base"]["ref"] != base
            or len(files) != pr["changed_files"]
        ):
            raise RuntimeBoundaryError("verification_head_mismatch")
        target_locales = {
            locale
            for item in files
            for locale in ("ru", "en")
            if item["filename"].startswith(f"ydb/docs/{locale}/")
        }
        if len(target_locales) != 1:
            raise RuntimeBoundaryError("verification_direction_missing")
        self.verification_direction = (
            Direction.RU_TO_EN if target_locales == {"en"} else Direction.EN_TO_RU
        )
        self.verification_target_paths = tuple(item.path for item in target_inventory.files)
        return snapshot

    def snapshot_continue(self, checkpoint: ContinuationCheckpoint, /) -> ImmutableRunSnapshot:
        """Restore scope inputs from saved refs/inventory, never today's PR file list.

        The authoritative document content and the PR diff base/head are pinned
        by the checkpoint. Current PR metadata supplies identity only.
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
        inventory = checkpoint.source_inventory
        if inventory.source_base_sha is None or inventory.source_head_sha is None:
            raise RuntimeBoundaryError("source_inventory_provenance_missing")
        self.source_base_snapshot = SnapshotRef(repository, inventory.source_base_sha)
        pr_snapshot = SnapshotRef(repository, inventory.source_head_sha)
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
        self.source_change_snapshot = pr_snapshot
        # Current target is an identity/publication parent only. Metadata starts
        # at the saved base and source, including for a previously merged PR.
        self.metadata_snapshot = base
        self.context = PublicationContext(
            self.github.repository,
            checkpoint.translation_branch,
            pr.base_branch,
            pr.base_branch,
            checkpoint.target_sha or checkpoint.base_sha,
            expected_branch_head=head,
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

    def report_failure(self, source_pr_number: int, diagnostic: str, /) -> None:
        from ydbdoc_review_ng.runtime_content import Limits

        configured_limit = None
        if diagnostic in {
            "dependency_file_limit_exceeded",
            "source_character_limit_exceeded",
        }:
            limits = Limits(self.source.environment)
            configured_limit = (
                limits.files
                if diagnostic == "dependency_file_limit_exceeded"
                else limits.characters
            )
        QAReporter(
            self.source.github,
            self.publisher,
            lambda: ReportContext(
                self.source.snapshots.source_snapshot.commit_sha,
                self.source.snapshots.source_snapshot.commit_sha,
                self.models.cost,
            ),
        ).report_failure(source_pr_number, diagnostic, configured_limit)

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
            # REQUIREMENTS §4.2: zero-commit RED still reports on the source PR.
            if review.final.verdict is not Verdict.RED:
                return
            report_context = ReportContext(
                self.source.snapshots.source_snapshot.commit_sha,
                self.source.snapshots.source_snapshot.commit_sha,
                self.models.cost,
                source_pr_number=self.source.source_pr,
            )
            body = render_report(review, report_context) + "\n" + QA_MARKER
            existing = next(
                (
                    comment
                    for comment in self.source.github.list_comments(self.source.source_pr)
                    if comment.authored_by_publisher and QA_MARKER in comment.body
                ),
                None,
            )
            if existing is None:
                self.source.github.create_comment(self.source.source_pr, body)
            else:
                self.source.github.update_comment(self.source.source_pr, existing.id, body)
            return
        head = self.source.github.head(branch)
        if (
            mode is Mode.DOC_CONTINUE
            and self.publisher.noop
            and self.source.continue_target_sha is None
            and head is None
        ):
            # §7: one current QA comment always reflects the final arbiter verdict,
            # including GREEN/YELLOW continue after a prior RED with target_sha=null.
            report_context = ReportContext(
                self.source.snapshots.source_snapshot.commit_sha,
                self.source.snapshots.source_snapshot.commit_sha,
                self.models.cost,
                source_pr_number=self.source.source_pr,
            )
            body = render_report(review, report_context) + "\n" + QA_MARKER
            existing = next(
                (
                    comment
                    for comment in self.source.github.list_comments(self.source.source_pr)
                    if comment.authored_by_publisher and QA_MARKER in comment.body
                ),
                None,
            )
            if existing is None:
                self.source.github.create_comment(self.source.source_pr, body)
            else:
                self.source.github.update_comment(self.source.source_pr, existing.id, body)
            return
        if head != commit_sha:
            raise RuntimeBoundaryError("report_head_changed")
        reporter = QAReporter(
            self.source.github,
            self.publisher,
            lambda: ReportContext(
                self.source.snapshots.source_snapshot.commit_sha,
                commit_sha,
                self.models.cost,
                self.source.probable_duplicates,
                self.source.source_pr,
            ),
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

    def report_failure(self, request: TranslateWorkflowInput, diagnostic: str, /) -> None:
        self._workflows.report_failure(request, diagnostic)

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
