from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest

from ydbdoc_review_ng.policy_review.runtime import prepare_review, queue_review, run_worker

from .helpers import response, semantic_json
from .runtime_fakes import FakeGitHub, MemoryStore, trigger
from .test_debounce import FakeTime

pytestmark = pytest.mark.unit
ENV = {"YDBDOC_REVIEW_MAX_RUN_COST_RUB": "100", "YANDEX_API_KEY": "fake",
       "YANDEX_FOLDER_ID": "fake"}


def test_duplicate_head_cannot_enter_concurrency_and_cancel_original_run() -> None:
    github, store = FakeGitHub(), MemoryStore()
    assert queue_review(trigger(), "owner", github, store, ENV) == "admitted"
    assert queue_review(trigger(), "duplicate", github, store, ENV) == "duplicate"
    assert prepared(github, store).status == "ready"


def test_untrusted_manual_operator_does_not_enter_concurrency() -> None:
    github, store = FakeGitHub(), MemoryStore()
    bad = replace(trigger("doc_review"), actor="external")
    assert queue_review(bad, "owner", github, store, ENV) == "operator_not_authorized"
    assert not store.queued


def prepared(github: FakeGitHub, store: MemoryStore, *, owner: str = "owner"):
    timer = FakeTime()
    return prepare_review(trigger(), owner, github, store, ENV, clock=timer.clock, wait=timer.wait)


def test_model_runs_only_after_quiet_period_and_durable_reservation() -> None:
    github, store, timer = FakeGitHub(), MemoryStore(), FakeTime()
    plan = prepare_review(trigger(), "owner", github, store, ENV,
                          clock=timer.clock, wait=timer.wait)
    assert timer.now == 300 and plan.status == "ready"

    def transport(request):
        assert store.events["owner"][1].kind == "reserved"
        return response(semantic_json())

    report = run_worker(plan, github, store, ENV, transport)
    assert report["cost_rub"] == "0.1"
    assert report["status"] == "incomplete"  # Link coverage is still explicitly incomplete.
    assert not store.leases


def test_external_unapproved_never_waits_prepares_or_claims() -> None:
    github, store, timer = FakeGitHub(external=True), MemoryStore(), FakeTime()
    plan = prepare_review(trigger(), "owner", github, store, ENV,
                          clock=timer.clock, wait=timer.wait)
    assert plan.status == "waiting_for_approval"
    assert timer.now == 0 and github.snapshots == 0 and not store.leases


def test_permissions_are_rechecked_after_wait() -> None:
    github, store, timer = FakeGitHub(), MemoryStore(), FakeTime()

    def wait(seconds: float) -> None:
        timer.wait(seconds)
        if timer.now == 300:
            github.members.clear()

    plan = prepare_review(trigger(), "owner", github, store, ENV, clock=timer.clock, wait=wait)
    assert plan.status == "waiting_for_approval" and not store.leases


def test_head_update_while_waiting_does_not_prepare_old_snapshot() -> None:
    github, store, timer = FakeGitHub(), MemoryStore(), FakeTime()

    def wait(seconds: float) -> None:
        timer.wait(seconds)
        github.pr = replace(github.pr, head_sha="c" * 40)

    assert prepare_review(trigger(), "owner", github, store, ENV,
                          clock=timer.clock, wait=wait).status == "superseded"
    assert github.snapshots == 0 and not store.leases


def test_zero_budget_disables_paid_stage_without_waiting() -> None:
    github, store, timer = FakeGitHub(), MemoryStore(), FakeTime()
    assert prepare_review(trigger(), "owner", github, store,
                          {"YDBDOC_REVIEW_MAX_RUN_COST_RUB": "0"},
                          clock=timer.clock, wait=timer.wait).status == "paid_review_disabled"
    assert timer.now == 0 and not store.leases


def test_many_contenders_only_one_owns_pr_and_other_pr_can_proceed() -> None:
    store = MemoryStore()
    def claim(n: int) -> bool:
        store.queue(42, "a" * 40, str(n), str(n))
        return store.claim(42, "a" * 40, str(n), str(n))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(20)))
    assert sum(results) == 1
    store.queue(43, "b" * 40, "other", "other")
    assert store.claim(43, "b" * 40, "other", "other")


def test_old_head_or_lost_lease_before_call_sends_no_paid_request() -> None:
    for updated in (True, False):
        github, store = FakeGitHub(), MemoryStore()
        plan = prepared(github, store)
        if updated:
            github.pr = replace(github.pr, head_sha="c" * 40)
        else:
            store.leases.clear()
        report = run_worker(plan, github, store, ENV, lambda _: pytest.fail("paid call"))
        assert report["status"] == ("superseded" if updated else "review_lease_lost")
        assert report["cost_rub"] == "0" and not store.events


def test_head_update_between_retries_prevents_second_paid_request() -> None:
    github, store = FakeGitHub(), MemoryStore()
    plan = prepared(github, store)
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        github.pr = replace(github.pr, head_sha="c" * 40)
        return response("invalid json")

    report = run_worker(plan, github, store, ENV, transport)
    assert calls == 1 and report["status"] == "superseded" and report["cost_rub"] == "0.1"


def test_automatic_duplicate_is_skipped_but_manual_attempt_has_new_budget() -> None:
    github, store = FakeGitHub(), MemoryStore()
    run_worker(prepared(github, store), github, store, ENV, lambda _: response(semantic_json()))
    assert prepared(github, store, owner="retry").status == "duplicate"
    timer = FakeTime()
    manual = prepare_review(trigger("doc_review", event_id="github-run:124"), "manual", github,
                            store, ENV, clock=timer.clock, wait=timer.wait)
    assert manual.status == "ready" and timer.now == 300
    report = run_worker(manual, github, store, ENV, lambda _: response(semantic_json(), Decimal("0.2")))
    assert report["cost_rub"] == "0.2"


def test_unknown_billable_outcome_keeps_pr_locked_after_worker_stops() -> None:
    github, store = FakeGitHub(), MemoryStore()
    report = run_worker(prepared(github, store), github, store, ENV,
                        lambda _: response(semantic_json(), None))
    # No usage/cost uncertainty can be treated as a free cancelled run.
    # This fixture has known usage, so force the actual unknown transport path instead.
    assert report["cost_rub"] is not None
    github, store = FakeGitHub(), MemoryStore()

    def fail(request):
        raise RuntimeError("secret transport payload")

    report = run_worker(prepared(github, store), github, store, ENV, fail)
    assert report["cost_rub"] is None and store.leases
    assert not store.claim(42, "a" * 40, "manual:new", "new-owner")
