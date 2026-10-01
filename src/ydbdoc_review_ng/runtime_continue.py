"""Read-only operator admission. Audit lifecycle and replay belong to the caller.

An admission result is not replay validation: the later workflow must validate
the original job and rebuild authoritative plans using the saved immutable SHAs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from ydbdoc_review_ng.continuation import (
    AcceptedDocument,
    AcceptedMap,
    ContinuationStage,
    ContinuationStateError,
    RestoredPlan,
    checkpoint_scope_sha256,
    validate_restored_documents,
)
from ydbdoc_review_ng.domain import GitSha, RepoPath, SnapshotRef
from ydbdoc_review_ng.persistence import ContinuationCheckpoint
from ydbdoc_review_ng.runtime_github import GitHubBackend, RuntimeBoundaryError
from ydbdoc_review_ng.scope import FileOperation
from ydbdoc_review_ng.translation_plan import (
    PathKind,
    TranslationPlanError,
    classify_path,
    translation_plan_sha256,
)

if TYPE_CHECKING:
    from ydbdoc_review_ng.runtime_content import (
        FrozenPreparation,
        FrozenSourcePlans,
        RuntimeContent,
    )


class CheckpointReader(Protocol):
    def load_checkpoint(
        self,
        pr_number: int,
        /,
        *,
        now: datetime,
        source_sha: GitSha | None = None,
        target_sha: GitSha | None = None,
    ) -> ContinuationCheckpoint: ...


@dataclass(frozen=True, slots=True)
class ContinueTrigger:
    label_event_id: int
    label_actor: str
    labeled_at: datetime
    comment_id: int
    comment_author: str
    commented_at: datetime
    operator_context: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ContinueAdmission:
    trigger_pr: int
    source_pr: int
    trigger: ContinueTrigger
    checkpoint: ContinuationCheckpoint


@dataclass(frozen=True, slots=True)
class ContinueReplay:
    preparation: FrozenPreparation
    plans: FrozenSourcePlans | None
    accepted_documents: tuple[AcceptedDocument, ...]
    accepted_maps: tuple[AcceptedMap, ...]


def _load_accepted_from_branch(
    content: RuntimeContent,
    plans: FrozenSourcePlans,
    target_sha: GitSha,
    paths: set[RepoPath],
) -> tuple[AcceptedDocument, ...]:
    published = SnapshotRef(plans.preparation.snapshots.source_snapshot.repository, target_sha)
    accepted: list[AcceptedDocument] = []
    for path in sorted(paths, key=lambda item: item.value):
        raw = content.source.github.read_bytes(published, path)
        if raw is None:
            continue
        try:
            accepted.append(AcceptedDocument(path, raw.decode("utf-8")))
        except UnicodeError as error:
            raise ContinuationStateError() from error
    return tuple(accepted)


def replay_continue(
    content: RuntimeContent, checkpoint: ContinuationCheckpoint, /
) -> ContinueReplay:
    """Rebuild and validate saved plans. Candidate bytes come from branch target_sha."""
    snapshot = content.source.snapshot_continue(checkpoint)
    preparation = content.prepare_source(snapshot)
    state = checkpoint.state
    if state.stage is ContinuationStage.DIRECTION:
        return ContinueReplay(preparation, None, (), ())
    referenced = set(state.pending_paths) | set(state.review_paths)
    potential = next(
        (scope for scope in preparation.potential.scopes if scope.direction is state.direction),
        None,
    )
    if potential is None:
        raise ContinuationStateError()
    # The inventory freezes semantic no-ops and TOC deltas as well as Git facts.
    assert state.direction is not None
    classification = content.restored_classification(preparation, state.direction)
    try:
        plans = content.select_source(
            preparation,
            classification=classification,
            review_documents=state.stage is ContinuationStage.REVIEW,
        )
    except TranslationPlanError:
        raise ContinuationStateError() from None
    if (
        plans.manifest is None
        or tuple(entry.pair.target_path for entry in plans.manifest.entries)
        != checkpoint.scope_target_paths
        or checkpoint_scope_sha256(
            plans.manifest,
            checkpoint.source_inventory,
            translation_plan_sha256(plans.translation_plan),
        )
        != state.scope_sha256
    ):
        raise ContinuationStateError()
    restored = tuple(
        RestoredPlan(document.entry.pair.target_path, document.source, document.plan)
        for document in plans.documents
    )
    metadata_paths = (
        tuple(
            RepoPath(path)
            for path, _value in plans.fixed_files
            if classify_path(content.roots, RepoPath(path)).kind is PathKind.TOC
        )
        if state.stage is ContinuationStage.REVIEW
        else ()
    )
    asset_paths = (
        tuple(
            item.target_path
            for item in plans.translation_plan.inputs
            if item.target_path is not None
            and item.kind
            in {PathKind.ASSET, PathKind.REDIRECTS, PathKind.LOCALIZED_OTHER}
        )
        if state.stage is ContinuationStage.REVIEW
        else ()
    )
    validate_restored_documents(
        state, restored, metadata_paths=metadata_paths + asset_paths
    )
    required = {
        document.entry.pair.target_path
        for document in plans.documents
        if document.entry.operation is not FileOperation.RENAME_TARGET
    }
    reviewable = {document.entry.pair.target_path for document in plans.documents} | set(
        metadata_paths
    ) | set(asset_paths)
    if state.stage is ContinuationStage.TRANSLATION:
        if not set(state.pending_paths).issubset(required) or not referenced <= reviewable:
            raise ContinuationStateError()
        accepted_paths = required - set(state.pending_paths)
        if state.target_sha is None:
            if accepted_paths:
                raise ContinuationStateError()
            accepted_documents: tuple[AcceptedDocument, ...] = ()
        else:
            accepted_documents = _load_accepted_from_branch(
                content, plans, state.target_sha, accepted_paths
            )
            if {item.target_path for item in accepted_documents} != accepted_paths:
                raise ContinuationStateError()
    elif state.stage is ContinuationStage.REVIEW:
        if not set(state.review_paths).issubset(reviewable):
            raise ContinuationStateError()
        if state.target_sha is None:
            # §4.2 zero-commit RED: no translation branch yet; nothing accepted.
            accepted_documents = ()
        else:
            accepted_documents = _load_accepted_from_branch(
                content, plans, state.target_sha, reviewable
            )
            loaded = {item.target_path for item in accepted_documents}
            # Soft-published null targets (new missing pages) stay absent on the
            # branch and must remain continuable for critic as JSON null (§5.1/§5.3).
            missing = required - loaded
            if missing - set(state.review_paths):
                raise ContinuationStateError()
    else:
        raise ContinuationStateError()
    accepted_maps = content.restore_accepted_documents(plans, accepted_documents)
    return ContinueReplay(preparation, plans, accepted_documents, accepted_maps)


def _command_context(body: str) -> str | None:
    # Remove just the first line delimiter. All remaining characters are context.
    first, separator, context = body.partition("\n")
    if separator and first.endswith("\r"):
        first = first[:-1]
    return context if first == "/ydbdoc continue" else None


def select_continue_trigger(
    github: GitHubBackend, pr_number: int, allowed_actors: frozenset[str]
) -> ContinueTrigger:
    """Authorize the latest label and the latest eligible first-line command."""
    if type(pr_number) is not int or pr_number < 1:
        raise RuntimeBoundaryError("continue_pr_invalid")
    if not allowed_actors:
        raise RuntimeBoundaryError("actor_not_authorized")
    events = [
        event for event in github.read_issue_events(pr_number) if event.label == "doc_continue"
    ]
    if not events:
        raise RuntimeBoundaryError("continue_label_missing")
    if len({event.created_at for event in events}) != len(events):
        raise RuntimeBoundaryError("continue_label_ambiguous")
    latest = max(events, key=lambda event: event.created_at)
    if latest.event != "labeled":
        raise RuntimeBoundaryError("continue_label_missing")
    if latest.actor not in allowed_actors:
        raise RuntimeBoundaryError("actor_not_authorized")
    candidates = [
        (comment, context)
        for comment in github.read_operator_comments(pr_number)
        if comment.author in allowed_actors and comment.created_at < latest.created_at
        if (context := _command_context(comment.body)) is not None
    ]
    if not candidates:
        raise RuntimeBoundaryError("continue_command_missing")
    if len({comment.created_at for comment, _ in candidates}) != len(candidates):
        raise RuntimeBoundaryError("continue_command_ambiguous")
    comment, context = max(candidates, key=lambda candidate: candidate[0].created_at)
    # The API returns the current body, so a later edit cannot prove pre-label context.
    # Check after selection to avoid falling back to an older command.
    if comment.updated_at >= latest.created_at:
        raise RuntimeBoundaryError("continue_command_edited_after_label")
    if not context.strip():
        raise RuntimeBoundaryError("continue_context_empty")
    return ContinueTrigger(
        latest.event_id,
        latest.actor,
        latest.created_at,
        comment.comment_id,
        comment.author,
        comment.created_at,
        context,
    )


def resolve_continue_checkpoint(
    github: GitHubBackend, checkpoints: CheckpointReader, pr_number: int, *, now: datetime
) -> ContinuationCheckpoint:
    """Match PR provenance to stored source identity, never to a branch-name pattern."""
    pr = github.read_pull_request_identity(pr_number)
    if pr.base_repository != github.repository:
        raise RuntimeBoundaryError("repository_mismatch")
    provenance = pr.provenance
    source_pr = pr_number if provenance is None else provenance.source_pr
    checkpoint = checkpoints.load_checkpoint(
        source_pr,
        now=now,
        source_sha=None if provenance is None else provenance.source_sha,
        target_sha=None if provenance is None else pr.head_sha,
    )
    if checkpoint.source_pr != source_pr:
        raise RuntimeBoundaryError("continue_source_identity_mismatch")
    if provenance is not None and (
        source_pr == pr_number
        or pr.head_repository != github.repository
        or provenance.source_sha != checkpoint.source_sha
        or pr.head_branch != checkpoint.translation_branch
        or pr.head_sha != checkpoint.target_sha
    ):
        raise RuntimeBoundaryError("continue_translation_provenance_mismatch")
    try:
        target = github.head(checkpoint.translation_branch)
    except (KeyError, TypeError, ValueError):
        raise RuntimeBoundaryError("continue_translation_head_invalid") from None
    if target != checkpoint.target_sha:
        raise RuntimeBoundaryError("continue_translation_head_mismatch")
    return checkpoint


def admit_continue(
    github: GitHubBackend,
    checkpoints: CheckpointReader,
    pr_number: int,
    allowed_actors: frozenset[str],
    *,
    now: datetime,
) -> ContinueAdmission:
    """Only read effects. The caller creates/terminalizes the mandatory audit job."""
    trigger = select_continue_trigger(github, pr_number, allowed_actors)
    checkpoint = resolve_continue_checkpoint(github, checkpoints, pr_number, now=now)
    return ContinueAdmission(pr_number, checkpoint.source_pr, trigger, checkpoint)
