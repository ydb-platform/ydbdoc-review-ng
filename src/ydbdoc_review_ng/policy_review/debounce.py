"""Cheap readiness gate before snapshots, Docker workers and paid model calls."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ydbdoc_review_ng.policy_review.types import ReviewError, sha

QUIET_SECONDS = 300
POLL_SECONDS = 15


@dataclass(frozen=True, slots=True)
class PullRequestState:
    head_sha: str
    open: bool
    draft: bool

    def __post_init__(self) -> None:
        sha(self.head_sha)
        if type(self.open) is not bool or type(self.draft) is not bool:
            raise ReviewError("invalid_review_pr_state")


@dataclass(frozen=True, slots=True)
class DebounceResult:
    status: Literal["ready", "superseded", "cancelled"]
    head_sha: str


def wait_for_quiet_head(
    expected_head: str,
    read_state: Callable[[], PullRequestState],
    *,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> DebounceResult:
    """Wait at least five minutes for this event's head; never adopt another head.

    The trusted caller starts this gate upon handling a relevant PR event.
    Another event owns a changed head and its own full quiet period. Delayed
    runners may wait longer than five minutes after the push, never less.
    This gate is not admission, deduplication or a cross-process lock: callers
    must enforce those separately and recheck head/admission before paid calls.
    """
    head = sha(expected_head)
    deadline = clock() + QUIET_SECONDS
    while True:
        try:
            state = read_state()
            if not isinstance(state, PullRequestState):
                raise ReviewError("invalid_review_pr_state")
        except Exception:  # noqa: BLE001 - API errors can include credentials/payloads.
            raise ReviewError("review_pr_state_unavailable") from None
        if not state.open or state.draft:
            return DebounceResult("cancelled", head)
        if state.head_sha != head:
            return DebounceResult("superseded", head)
        remaining = deadline - clock()
        if remaining <= 0:
            return DebounceResult("ready", head)
        wait(min(POLL_SECONDS, remaining))
