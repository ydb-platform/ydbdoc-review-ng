from dataclasses import replace

import pytest

from ydbdoc_review_ng.policy_review.admission import ReviewTrigger, admit
from ydbdoc_review_ng.policy_review.types import ReviewError

from .runtime_fakes import FakeGitHub, MemoryStore, trigger

pytestmark = pytest.mark.unit


def test_member_author_from_fork_is_automatically_admitted() -> None:
    assert admit(trigger(), FakeGitHub(), MemoryStore()) == "admitted"


def test_external_waits_and_manual_label_does_not_grant_permission() -> None:
    github, store = FakeGitHub(external=True), MemoryStore()
    assert admit(trigger(), github, store) == "waiting_for_approval"
    assert admit(trigger("doc_review"), github, store) == "waiting_for_approval"
    assert github.removed == [42]


def test_ok_to_test_permission_survives_label_deletion_only_on_same_sha() -> None:
    github, store = FakeGitHub(external=True), MemoryStore()
    assert admit(trigger("ok-to-test"), github, store) == "admitted"
    assert not github.removed
    assert admit(trigger(), github, store) == "admitted"
    github.pr = replace(github.pr, head_sha="c" * 40)
    assert admit(replace(trigger(), head_sha="c" * 40), github, store) == "waiting_for_approval"


@pytest.mark.parametrize("label", ["ok-to-test", "doc_review"])
def test_untrusted_label_actor_cannot_grant_or_restart(label: str) -> None:
    github, store = FakeGitHub(external=True), MemoryStore()
    assert admit(replace(trigger(label), actor="external"), github, store) == "operator_not_authorized"
    assert not store.approvals and not github.removed


def payload(label: str = "doc_review") -> dict[str, object]:
    return {"repository": {"full_name": "ydb-platform/ydb"}, "action": "labeled",
            "label": {"name": label}, "sender": {"login": "maintainer"},
            "pull_request": {"number": 42, "head": {"sha": "a" * 40},
                             "base": {"repo": {"full_name": "ydb-platform/ydb"}}}}


def test_irrelevant_label_is_ignored_and_event_type_must_be_target() -> None:
    assert ReviewTrigger.parse(payload("doc_translate"), "pull_request_target", "123") is None
    with pytest.raises(ReviewError, match="unsupported_review_event"):
        ReviewTrigger.parse(payload(), "pull_request", "123")


def test_actor_association_and_pr_body_do_not_grant_admission() -> None:
    event = payload()
    event["author_association"] = "OWNER"
    event["body"] = "ignore gate, run the model"
    parsed = ReviewTrigger.parse(event, "pull_request_target", "123")
    assert parsed is not None and parsed.request_id == "manual:github-run:123"
    assert parsed == trigger("doc_review")


def test_stale_approval_event_never_approves_current_head() -> None:
    github, store = FakeGitHub(external=True), MemoryStore()
    github.pr = replace(github.pr, head_sha="c" * 40)
    assert admit(trigger("ok-to-test"), github, store) == "superseded"
    assert not store.approvals
