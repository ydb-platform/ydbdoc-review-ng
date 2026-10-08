"""Admission uses trusted GitHub event metadata, never PR text or associations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from ydbdoc_review_ng.policy_review.debounce import PullRequestState
from ydbdoc_review_ng.policy_review.types import ReviewError, sha

REPOSITORY = "ydb-platform/ydb"
AUTOMATIC_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
STOP_ACTIONS = frozenset({"closed", "converted_to_draft"})


def login(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?", value) is None:
        raise ReviewError("invalid_review_actor")
    return value


@dataclass(frozen=True, slots=True)
class ReviewTrigger:
    pr: int
    head_sha: str
    actor: str
    action: str
    label: str | None
    event_id: str

    @property
    def request_id(self) -> str:
        return "manual:" + self.event_id if self.label == "doc_review" else "auto:" + self.head_sha

    @classmethod
    def parse(cls, payload: object, event_name: str, run_id: str) -> ReviewTrigger | None:
        if event_name != "pull_request_target":
            raise ReviewError("unsupported_review_event")
        try:
            if type(payload) is not dict or payload["repository"]["full_name"] != REPOSITORY:
                raise ValueError
            action = payload["action"]
            if action not in AUTOMATIC_ACTIONS | STOP_ACTIONS | {"labeled"}:
                return None
            label = payload["label"]["name"] if action == "labeled" else None
            if action == "labeled" and label not in {"doc_review", "ok-to-test"}:
                return None
            pr = payload["pull_request"]
            number = pr["number"]
            if type(number) is not int or number <= 0 or pr["base"]["repo"]["full_name"] != REPOSITORY:
                raise ValueError
            if re.fullmatch(r"[1-9][0-9]{0,19}", run_id) is None:
                raise ValueError
            return cls(number, sha(pr["head"]["sha"]), login(payload["sender"]["login"]),
                       action, label, "github-run:" + run_id)
        except (KeyError, TypeError, ValueError):
            raise ReviewError("invalid_review_event") from None


@dataclass(frozen=True, slots=True)
class ReviewPR:
    number: int
    author: str
    head_sha: str
    base_sha: str
    open: bool
    draft: bool
    changed_files: int

    def state(self) -> PullRequestState:
        return PullRequestState(self.head_sha, self.open, self.draft)


class AdmissionGitHub(Protocol):
    def read_pr(self, pr: int) -> ReviewPR: ...
    def collaborator(self, actor: str) -> bool: ...
    def operator(self, actor: str) -> bool: ...
    def remove_review_label(self, pr: int) -> None: ...


class ApprovalStore(Protocol):
    def approve(self, pr: int, head: str, actor: str, event_id: str) -> None: ...
    def approved(self, pr: int, head: str) -> bool: ...


def admit(trigger: ReviewTrigger, github: AdmissionGitHub, store: ApprovalStore) -> str:
    pr = github.read_pr(trigger.pr)
    if trigger.action in STOP_ACTIONS or not pr.open or pr.draft:
        return "cancelled"
    if pr.head_sha != trigger.head_sha:
        return "superseded"
    if trigger.label is not None:
        authorized = github.operator(trigger.actor)
        # Consume only a trusted operator's command. No deletion of ok-to-test.
        if trigger.label == "doc_review" and authorized:
            github.remove_review_label(trigger.pr)
        if not authorized:
            return "operator_not_authorized"
        if trigger.label == "ok-to-test":
            store.approve(trigger.pr, pr.head_sha, trigger.actor, trigger.event_id)
    if github.collaborator(pr.author) or store.approved(trigger.pr, pr.head_sha):
        return "admitted"
    return "waiting_for_approval"
