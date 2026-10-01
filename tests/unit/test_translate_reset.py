"""Fresh doc_translate must delete the old translation branch and close checkpoints (§5.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ydbdoc_review_ng.application.workflows import (
    AuthorizedRun,
    ImmutableRunSnapshot,
    LinearWorkflows,
    TranslateWorkflowInput,
    WorkflowCandidate,
)
from ydbdoc_review_ng.domain import GitSha, Mode
from ydbdoc_review_ng.quality import Verdict
from ydbdoc_review_ng.runtime_github import GitHubBackend, RuntimeBoundaryError


NOW = datetime(2026, 9, 21, 9, tzinfo=UTC)
SOURCE_SHA = GitSha("a" * 40)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.heads: dict[str, str | None] = {"translation/pr-42": "b" * 40}

    def __call__(self, method: str, path: str, payload: object) -> object:
        self.calls.append((method, path, payload))
        prefix = "/repos/ydb-platform/ydb/git/ref/heads/"
        refs_prefix = "/repos/ydb-platform/ydb/git/refs/heads/"
        if method == "GET" and path.startswith(prefix):
            branch = path[len(prefix) :]
            sha = self.heads.get(branch)
            return None if sha is None else {"object": {"sha": sha}}
        if method == "DELETE" and path.startswith(refs_prefix):
            branch = path[len(refs_prefix) :]
            self.heads[branch] = None
            return None
        if method == "DELETE" and "/labels/" in path:
            return None
        return {}


def test_delete_branch_removes_existing_translation_ref() -> None:
    transport = RecordingTransport()
    backend = GitHubBackend(transport)

    backend.delete_branch("translation/pr-42")

    assert ("DELETE", "/repos/ydb-platform/ydb/git/refs/heads/translation/pr-42", None) in (
        (method, path, payload) for method, path, payload in transport.calls
    )
    assert transport.heads["translation/pr-42"] is None


def test_delete_branch_is_noop_when_ref_missing() -> None:
    transport = RecordingTransport()
    transport.heads["translation/pr-42"] = None
    backend = GitHubBackend(transport)

    backend.delete_branch("translation/pr-42")

    assert not any(method == "DELETE" for method, _, _ in transport.calls)


def test_remove_label_deletes_trigger_label() -> None:
    transport = RecordingTransport()
    backend = GitHubBackend(transport)

    backend.remove_label(42, "doc_translate")

    assert (
        "DELETE",
        "/repos/ydb-platform/ydb/issues/42/labels/doc_translate",
        None,
    ) in ((method, path, payload) for method, path, payload in transport.calls)


def test_remove_label_surfaces_mutation_transport_failure() -> None:
    """§5.3: label mutation 403/5xx/network must terminate, not be swallowed."""

    def unavailable(method: str, path: str, payload: object) -> object:
        raise RuntimeBoundaryError("github_request_failed")

    with pytest.raises(RuntimeBoundaryError, match="github_request_failed"):
        GitHubBackend(unavailable).remove_label(42, "doc_translate")


class FakeClock:
    def now(self) -> datetime:
        return NOW


class RecordingPersistence:
    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []
        self.finished: list[str] = []

    def start_job(self, mode, /, *, pr_number, source_sha, target_sha, started_at):
        return "job-42"

    def finish_job(self, job_id, status, /, *, error, finished_at, target_sha=None):
        self.finished.append(status.value)

    def check_daily_budget(self, mode, /, *, limit_rub, now):
        return None

    def bind_job_snapshot(self, job_id, /, *, source_sha, target_sha):
        return None

    def save_checkpoint(self, checkpoint, /, *, now):
        raise AssertionError("unexpected checkpoint")

    def activate_checkpoint(self, checkpoint, /, *, now):
        raise AssertionError("unexpected activate")

    def validate_checkpoint_job(self, checkpoint, /):
        return None

    def load_checkpoint(self, pr_number, /, *, now, source_sha=None, target_sha=None):
        raise AssertionError("unexpected load")

    def close_checkpoint(self, continuation_id, /):
        raise AssertionError("unexpected single close")

    def close_open_checkpoints(self, *, source_pr: int, translation_branch: str) -> None:
        self.closed.append((source_pr, translation_branch))

    def consume_checkpoint(self, checkpoint, job_id, /, *, now):
        return None

    def finish_job_reconciled(
        self, job_id, status, /, *, error, finished_at, target_sha
    ):
        self.finished.append(status.value)


class FakeSource:
    def __init__(self) -> None:
        self.reset_calls: list[str] = []
        self.accept_calls: list[int] = []

    def authorize_translate(self, request):
        self.accept_calls.append(request.pr_number)
        return AuthorizedRun(Mode.DOC_TRANSLATE, "translation/pr-42", None, request)

    def snapshot_translate(self, authorization):
        self.reset_calls.append(authorization.branch)
        return ImmutableRunSnapshot(
            Mode.DOC_TRANSLATE, SOURCE_SHA, None, authorization.branch, object()
        )

    def authorize_verify(self, request):
        raise AssertionError("verify unused")

    def snapshot_verify(self, authorization):
        raise AssertionError("verify unused")

    def authorize_continue(self, pr_number, checkpoints, /, *, now):
        raise AssertionError("continue unused")


class FakeContent:
    def prepare_translation(self, snapshot):
        return WorkflowCandidate(b"candidate", object())

    def validate_candidate(self, snapshot, candidate):
        return None

    def review_checkpoint(self, snapshot, review, target_sha):
        raise AssertionError("unexpected checkpoint")

    def load_verification_candidate(self, snapshot):
        raise AssertionError("unused")

    def replay_continuation(self, checkpoint):
        raise AssertionError("unused")

    def prepare_continuation(self, replay, checkpoint, /, *, operator_context):
        raise AssertionError("unused")


class FakeReviewer:
    def review(self, snapshot, candidate):
        return type(
            "Review",
            (),
            {
                "final": type("Final", (), {"verdict": Verdict.GREEN})(),
                "final_candidate": candidate.content,
                "repair_applied": False,
                "accepted_maps": None,
            },
        )()


class FakePublisher:
    noop = False

    def publish(self, snapshot, candidate):
        return GitSha("c" * 40)


class FakeReporter:
    def update_current_pr(self, **kwargs):
        return None

    def report_failure(self, pr_number, diagnostic):
        return None


def test_doc_translate_closes_open_checkpoints_before_snapshot() -> None:
    persistence = RecordingPersistence()
    source = FakeSource()
    workflows = LinearWorkflows(
        clock=FakeClock(),
        persistence=persistence,
        source=source,
        content=FakeContent(),
        reviewer=FakeReviewer(),
        publisher=FakePublisher(),
        reporter=FakeReporter(),
    )

    result = workflows.doc_translate(
        TranslateWorkflowInput(42, SOURCE_SHA, Decimal(100))
    )

    assert result.verdict is Verdict.GREEN
    assert persistence.closed == [(42, "translation/pr-42")]
    assert source.reset_calls == ["translation/pr-42"]
    assert source.accept_calls == [42]
