"""Publication of a previously validated immutable plan through an injected backend.

Composition calls validate_candidate from ContentWorkflowPort.validate_candidate.
The plan builder supplies snapshot-derived before/after bytes and known TOC facts;
the validator checks the complete plan before it can reach commit or push.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from ydbdoc_review_ng.application import ImmutableRunSnapshot, WorkflowCandidate
from ydbdoc_review_ng.domain import GitSha, RepoPath
from ydbdoc_review_ng.scope import FileOperation


class PublicationError(RuntimeError):
    """Fixed diagnostics only; never retain backend exception payloads."""


@dataclass(frozen=True, slots=True)
class PublicationContext:
    repository: str
    branch: str
    base: str
    source_base: str
    current_head: GitSha
    branch_must_exist: bool = False


@dataclass(frozen=True, slots=True)
class FileChange:
    path: RepoPath
    before: bytes | None = field(repr=False)
    after: bytes | None = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.path) is not RepoPath or any(
            value is not None and type(value) is not bytes for value in (self.before, self.after)
        ):
            raise PublicationError("invalid_file_change")


@dataclass(frozen=True, slots=True)
class MetadataChange:
    kind: Literal["toc_add", "toc_replace", "redirect"]
    old_path: RepoPath | None
    new_path: RepoPath

    def __post_init__(self) -> None:
        if (
            self.kind not in {"toc_add", "toc_replace", "redirect"}
            or type(self.new_path) is not RepoPath
            or (self.kind == "toc_add" and self.old_path is not None)
            or (self.kind != "toc_add" and type(self.old_path) is not RepoPath)
            or self.old_path == self.new_path
        ):
            raise PublicationError("invalid_metadata_change")


def metadata_plan(
    operation: FileOperation,
    target: RepoPath,
    *,
    source_is_new: bool = False,
    source_toc_reachable: bool = False,
    old_target: RepoPath | None = None,
) -> tuple[MetadataChange, ...]:
    if operation in {FileOperation.RENAME_TARGET, FileOperation.RENAME_TARGET_AND_TRANSLATE}:
        return (
            MetadataChange("toc_replace", old_target, target),
            MetadataChange("redirect", old_target, target),
        )
    if operation is FileOperation.TRANSLATE and source_is_new and source_toc_reachable:
        return (MetadataChange("toc_add", None, target),)
    return ()


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    files: tuple[FileChange, ...]
    metadata: tuple[MetadataChange, ...]
    existing_redirects: tuple[MetadataChange, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.files) is not tuple
            or type(self.metadata) is not tuple
            or any(type(item) is not FileChange for item in self.files)
            or any(type(item) is not MetadataChange for item in self.metadata)
            or type(self.existing_redirects) is not tuple
            or any(
                type(item) is not MetadataChange or item.kind != "redirect"
                for item in self.existing_redirects
            )
            or len({item.path for item in self.files}) != len(self.files)
        ):
            raise PublicationError("invalid_plan")
        redirects = (
            tuple(item for item in self.metadata if item.kind == "redirect")
            + self.existing_redirects
        )
        if {item.old_path for item in redirects} & {item.new_path for item in redirects}:
            raise PublicationError("redirect_chain")
        if len({item.old_path for item in redirects}) != len(redirects):
            raise PublicationError("duplicate_redirect")

    @property
    def changed(self) -> bool:
        return bool(self.metadata) or any(item.before != item.after for item in self.files)


class GitBackend(Protocol):
    """Commit applies this exact plan atomically on context.current_head.

    The backend materializes the supplied TOC/redirect operations and pushes
    with an expected-head check. The builder supplies relevant existing
    redirects for chain validation. The backend must not discover additional
    files or execute checkout hooks.
    Transport and metadata file serialization belong to deployment composition.
    """

    def commit(self, context: PublicationContext, plan: PublicationPlan, /) -> GitSha: ...
    def push(self, context: PublicationContext, sha: GitSha, /) -> None: ...
    def find_pr(self, repository: str, branch: str, base: str, /) -> int | None: ...
    def create_pr(self, context: PublicationContext, sha: GitSha, /) -> int: ...
    def update_pr(self, pr_number: int, context: PublicationContext, sha: GitSha, /) -> None: ...


PlanBuilder = Callable[[ImmutableRunSnapshot, WorkflowCandidate], PublicationPlan]
PlanValidator = Callable[[ImmutableRunSnapshot, WorkflowCandidate, PublicationPlan], None]


class GitPublicationAdapter:
    def __init__(self, backend: GitBackend, plan_builder: PlanBuilder, validator: PlanValidator):
        self._backend = backend
        self._plan_builder = plan_builder
        self._validator = validator
        self._validated: (
            tuple[ImmutableRunSnapshot, bytes, PublicationPlan, PublicationContext] | None
        ) = None
        self._published_snapshot: ImmutableRunSnapshot | None = None
        self.context: PublicationContext | None = None
        self.pr_number: int | None = None
        self.noop = False

    @staticmethod
    def _context(snapshot: ImmutableRunSnapshot) -> PublicationContext:
        context = snapshot.context
        if (
            type(context) is not PublicationContext
            or context.repository != "ydb-platform/ydb"
            or context.base != context.source_base
            or not context.base
            or context.branch != snapshot.branch
            or context.branch == context.base
            or type(context.current_head) is not GitSha
        ):
            raise PublicationError("repository_or_base_mismatch")
        return replace(
            context, branch_must_exist=context.branch_must_exist or snapshot.target_sha is not None
        )

    def validate_candidate(
        self, snapshot: ImmutableRunSnapshot, candidate: WorkflowCandidate, /
    ) -> None:
        self._validated = None
        context = self._context(snapshot)
        if self._published_snapshot == snapshot and self.context is not None:
            context = self.context
        current_snapshot = replace(snapshot, context=context)
        try:
            plan = self._plan_builder(current_snapshot, candidate)
            if type(plan) is not PublicationPlan:
                raise PublicationError("invalid_plan")
            self._validator(current_snapshot, candidate, plan)
        except Exception:  # noqa: BLE001 - injected boundaries must not leak payloads.
            raise PublicationError("validation_failed") from None
        self._validated = (snapshot, candidate.content, plan, context)

    def publish(self, snapshot: ImmutableRunSnapshot, candidate: WorkflowCandidate, /) -> GitSha:
        self._context(snapshot)
        receipt = self._validated
        if receipt is None or receipt[:2] != (snapshot, candidate.content):
            raise PublicationError("unvalidated_changes")
        self._validated = None
        plan = receipt[2]
        context = receipt[3]
        self.context = context
        self.noop = not plan.changed and self._published_snapshot != snapshot
        if not plan.changed:
            return context.current_head
        try:
            sha = self._backend.commit(context, plan)
            if type(sha) is not GitSha:
                raise PublicationError("invalid_commit_sha")
            self._backend.push(context, sha)
            self.context = replace(context, current_head=sha, branch_must_exist=True)
            self._published_snapshot = snapshot
            return sha
        except Exception:  # noqa: BLE001 - backend exceptions can contain credentials.
            raise PublicationError("publication_failed") from None

    def ensure_pr(self, sha: GitSha, /) -> int | None:
        """Create/update the PR at the final reporting stage, after critic."""
        if self.noop:
            return None
        context = self.context
        if context is None or self._published_snapshot is None or context.current_head != sha:
            raise PublicationError("unpublished_commit")
        try:
            number = self._backend.find_pr(context.repository, context.branch, context.base)
            if number is None:
                number = self._backend.create_pr(context, sha)
            else:
                self._backend.update_pr(number, context, sha)
            self.pr_number = number
            return number
        except Exception:  # noqa: BLE001 - backend exceptions can contain credentials.
            raise PublicationError("pr_update_failed") from None
