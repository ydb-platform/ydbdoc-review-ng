from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from threading import Lock

from ydbdoc_review_ng.models import AttemptResult
from ydbdoc_review_ng.policy_review.admission import ReviewPR, ReviewTrigger
from ydbdoc_review_ng.policy_review.budget import BudgetEvent
from ydbdoc_review_ng.policy_review.github import ReviewGitHub
from ydbdoc_review_ng.policy_review.types import ReviewError, ReviewSnapshot

from .helpers import PATH, snapshot


def trigger(label: str | None = None, *, event_id: str = "github-run:123") -> ReviewTrigger:
    return ReviewTrigger(42, "a" * 40, "maintainer", "labeled" if label else "opened", label, event_id)


class MemoryStore:
    def __init__(self) -> None:
        self.approvals: set[tuple[int, str]] = set()
        self.leases: dict[int, tuple[str, str]] = {}
        self.claims: set[tuple[int, str]] = set()
        self.queued: dict[tuple[int, str], str] = {}
        self.events: dict[str, dict[int, BudgetEvent]] = {}
        self.attempts: list[AttemptResult] = []
        self.reports: dict[str, Mapping[str, object]] = {}
        self._lock = Lock()

    def approve(self, pr: int, head: str, actor: str, event_id: str) -> None:
        self.approvals.add((pr, head))

    def approved(self, pr: int, head: str) -> bool:
        return (pr, head) in self.approvals

    def claim(self, pr: int, head: str, request_id: str, owner: str) -> bool:
        with self._lock:
            if (pr in self.leases or (pr, request_id) in self.claims
                    or self.queued.get((pr, request_id)) != owner):
                return False
            self.leases[pr] = (owner, head)
            self.claims.add((pr, request_id))
            return True

    def queue(self, pr: int, head: str, request_id: str, owner: str) -> bool:
        with self._lock:
            key = (pr, request_id)
            if key in self.claims:
                return False
            if key in self.queued:
                return self.queued[key] == owner
            self.queued[key] = owner
            return True

    def owns(self, pr: int, head: str, owner: str) -> bool:
        return self.leases.get(pr) == (owner, head)

    def budget(self, owner: str, event: BudgetEvent) -> None:
        if event.kind == "reserved" and not any(o == owner for o, _ in self.leases.values()):
            raise ReviewError("review_lease_lost")
        self.events.setdefault(owner, {})[event.attempt_id] = event

    def attempt(self, owner: str, number: int, attempt: AttemptResult) -> None:
        self.attempts.append(attempt)

    def finish(self, pr: int, owner: str, report: Mapping[str, object]) -> None:
        self.reports[owner] = report
        if (not any(e.kind != "settled" for e in self.events.get(owner, {}).values())
                and self.leases.get(pr, (None, None))[0] == owner):
            del self.leases[pr]
        self.queued = {k: v for k, v in self.queued.items() if v != owner or k in self.claims}

    def configure(self, owner: str, metadata: Mapping[str, object]) -> None:
        self.reports[owner] = metadata


class FakeGitHub(ReviewGitHub):
    def __init__(self, *, external: bool = False) -> None:
        self.pr = ReviewPR(42, "external" if external else "member", "a" * 40,
                           "b" * 40, True, False, 1)
        self.members = {"member"}
        self.operators = {"maintainer"}
        self.removed: list[int] = []
        self.inventory = [{"filename": PATH, "status": "modified"}]
        self.snapshots = 0

    def read_pr(self, pr: int) -> ReviewPR:
        return self.pr

    def collaborator(self, actor: str) -> bool:
        return actor in self.members

    def operator(self, actor: str) -> bool:
        return actor in self.operators

    def remove_review_label(self, pr: int) -> None:
        self.removed.append(pr)

    def files(self, pr: ReviewPR) -> list[dict[str, str]]:  # type: ignore[override]
        return self.inventory

    def snapshot(self, pr: ReviewPR, files: list[dict[str, str]],  # type: ignore[override]
                 rules_sha: str | None = None) -> ReviewSnapshot:
        self.snapshots += 1
        return replace(snapshot(), head_sha=pr.head_sha)
