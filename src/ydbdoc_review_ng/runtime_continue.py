"""Read-only operator admission. Audit lifecycle and replay belong to the caller.

An admission result is not replay validation: the later workflow must validate
the original job and rebuild authoritative plans using the saved immutable SHAs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ydbdoc_review_ng.persistence import ContinuationCheckpoint
from ydbdoc_review_ng.runtime_github import GitHubBackend, RuntimeBoundaryError


class CheckpointReader(Protocol):
    def load_checkpoint(self, pr_number: int, /, *, now: datetime) -> ContinuationCheckpoint: ...


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
    checkpoint = checkpoints.load_checkpoint(source_pr, now=now)
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
