from collections.abc import Callable

import pytest

from ydbdoc_review_ng.policy_review.debounce import (
    DebounceResult,
    PullRequestState,
    wait_for_quiet_head,
)
from ydbdoc_review_ng.policy_review.types import ReviewError

pytestmark = pytest.mark.unit
HEAD = "a" * 40
NEW_HEAD = "b" * 40


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def run(timer: FakeTime, read: Callable[[], PullRequestState], head: str = HEAD) -> DebounceResult:
    return wait_for_quiet_head(head, read, clock=timer.clock, wait=timer.wait)


def test_stable_head_waits_five_minutes_and_checks_state_at_deadline() -> None:
    timer = FakeTime()
    reads: list[float] = []

    def read() -> PullRequestState:
        reads.append(timer.now)
        return PullRequestState(HEAD, True, False)

    assert run(timer, read) == DebounceResult("ready", HEAD)
    assert timer.now == 300
    assert reads[0] == 0 and reads[-1] == 300
    assert max(timer.waits) <= 15


@pytest.mark.parametrize("changed_at", [0, 60, 299, 300])
def test_new_head_prevents_old_launch_even_at_deadline(changed_at: int) -> None:
    timer = FakeTime()

    def read() -> PullRequestState:
        return PullRequestState(NEW_HEAD if timer.now >= changed_at else HEAD, True, False)

    assert run(timer, read).status == "superseded"
    assert timer.now <= 300


def test_burst_of_pushes_only_last_head_becomes_ready_after_full_wait() -> None:
    timer = FakeTime()
    heads = ["a" * 40, "b" * 40, "c" * 40]
    pushed_at = [0, 60, 120]

    def read() -> PullRequestState:
        index = max(i for i, pushed in enumerate(pushed_at) if pushed <= timer.now)
        return PullRequestState(heads[index], True, False)

    results = [run(timer, read, head) for head in heads]
    assert [r.status for r in results] == ["superseded", "superseded", "ready"]
    assert timer.now == 420


@pytest.mark.parametrize("is_open,draft", [(False, False), (True, True)])
def test_closed_or_draft_pr_stops_waiting(is_open: bool, draft: bool) -> None:
    timer = FakeTime()

    def read() -> PullRequestState:
        return PullRequestState(HEAD, True, False) if timer.now < 30 else (
            PullRequestState(HEAD, is_open, draft)
        )

    assert run(timer, read).status == "cancelled"
    assert timer.now == 30


def test_api_failure_at_deadline_does_not_allow_launch_or_leak_error() -> None:
    timer = FakeTime()

    def read() -> PullRequestState:
        if timer.now == 300:
            raise RuntimeError("private API payload")
        return PullRequestState(HEAD, True, False)

    with pytest.raises(ReviewError, match="^review_pr_state_unavailable$"):
        run(timer, read)


def test_slow_reads_count_toward_quiet_period() -> None:
    timer = FakeTime()

    def read() -> PullRequestState:
        timer.now += 10
        return PullRequestState(HEAD, True, False)

    assert run(timer, read).status == "ready"
    assert 300 <= timer.now <= 310


def test_invalid_expected_head_never_reads_api() -> None:
    with pytest.raises(ReviewError, match="invalid_snapshot_sha"):
        wait_for_quiet_head("invalid", lambda: pytest.fail("must not read API"))
