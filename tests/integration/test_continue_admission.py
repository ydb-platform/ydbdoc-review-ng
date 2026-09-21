from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ydbdoc_review_ng.persistence import PersistenceError, YdbPersistence
from ydbdoc_review_ng.runtime import RuntimeSource
from ydbdoc_review_ng.runtime_github import GitHubBackend, RuntimeBoundaryError

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
SOURCE_SHA = "a" * 40
BASE_SHA = "b" * 40
TARGET_SHA = "c" * 40
BRANCH = "arbitrary-saved-branch"


class AdmissionServices:
    def __init__(self):
        self.calls = []
        self.effects = []
        self.events = [
            {
                "id": 1,
                "event": "labeled",
                "label": {"name": "doc_continue"},
                "actor": {"login": "writer"},
                "created_at": "2026-09-22T11:00:00Z",
            }
        ]
        self.comments = [
            {
                "id": 2,
                "user": {"login": "writer"},
                "created_at": "2026-09-22T10:00:00Z",
                "updated_at": "2026-09-22T10:00:00Z",
                "body": "/ydbdoc continue\nprivate context\nsecond line\n",
            }
        ]
        self.row = {
            "continuation_id": "checkpoint-1",
            "job_id": "original-job",
            "source_pr": 42,
            "trigger_pr": 42,
            "source_sha": SOURCE_SHA,
            "base_sha": BASE_SHA,
            "translation_branch": BRANCH,
            "target_sha": TARGET_SHA,
            "source_inventory": b"[]",
            "scope_target_paths": b"[]",
            "stage": "direction",
            "status": "open",
            "created_at": NOW - timedelta(days=1),
            "state": b'{"state_version":1,"stage":"direction","direction":null,'
            b'"scope_sha256":null,"accepted_maps":{},"pending_paths":[], '
            b'"review_paths":[],"candidate_sha256":null}',
        }
        self.job = {
            "job_id": "original-job",
            "status": "failed",
            "error": "continuable_direction",
            "source_sha": SOURCE_SHA,
            "target_sha": TARGET_SHA,
        }
        repo = {"full_name": "ydb-platform/ydb"}
        self.prs = {
            42: {
                "number": 42,
                "body": "source PR",
                "head": {"ref": "translation/pr-999", "sha": SOURCE_SHA, "repo": repo},
                "base": {"ref": "main", "repo": repo},
            },
            52: {
                "number": 52,
                "body": f"<!-- ydbdoc-source-pr:42 -->\n<!-- ydbdoc-source-sha:{SOURCE_SHA} -->",
                "head": {"ref": BRANCH, "sha": TARGET_SHA, "repo": repo},
                "base": {"ref": "main", "repo": repo},
            },
        }
        self.head = TARGET_SHA

    def github(self, method, path, payload):
        self.calls.append((method, path))
        if method != "GET":
            self.effects.append((method, path))
            raise AssertionError("unexpected GitHub mutation")
        if "/events?" in path:
            return self.events
        if "/comments?" in path:
            return self.comments
        if "/pulls/" in path:
            return self.prs[int(path.rsplit("/", 1)[1])]
        if path.endswith("/git/ref/heads/" + BRANCH):
            return None if self.head is None else {"object": {"sha": self.head}}
        raise AssertionError(path)

    def execute(self, statement, parameters, /):
        self.calls.append(("YDB", dict(parameters)))
        if not statement.lstrip().startswith("SELECT"):
            self.effects.append(("YDB_WRITE", statement))
            raise AssertionError("admission must be read-only")
        if "/jobs`" in statement:
            return [self.job] if parameters["job_id"] == self.job["job_id"] else []
        return (
            [self.row]
            if parameters["pr_number"] in (self.row["source_pr"], self.row["trigger_pr"])
            else []
        )

    def forbidden(self, *args, **kwargs):
        self.effects.append(("MODEL_OR_WORKTREE", "unexpected"))
        raise AssertionError("admission must not execute models or worktree operations")


@pytest.fixture
def services(monkeypatch):
    result = AdmissionServices()
    monkeypatch.setattr("ydbdoc_review_ng.models.NativeYandexClient.invoke", result.forbidden)
    monkeypatch.setattr("subprocess.run", result.forbidden)
    monkeypatch.setattr(Path, "write_bytes", result.forbidden)
    monkeypatch.setattr(Path, "write_text", result.forbidden)
    return result


def admit(services, pr=42, allowed="writer", now=NOW):
    source = RuntimeSource(
        {"YDBDOC_ALLOWED_ACTORS": allowed, "GITHUB_TRIGGERING_ACTOR": "writer"},
        GitHubBackend(services.github),
    )
    return source.authorize_continue(pr, YdbPersistence(services), now=now)


@pytest.mark.parametrize("pr", [42, 52])
def test_source_and_translation_pr_resolve_same_checkpoint_without_branch_guessing(services, pr):
    result = admit(services, pr)
    assert result.source_pr == 42
    assert result.trigger_pr == pr
    assert result.checkpoint.continuation_id == "checkpoint-1"
    assert result.checkpoint.source_sha.value == SOURCE_SHA
    assert result.checkpoint.base_sha.value == BASE_SHA
    assert result.checkpoint.translation_branch == BRANCH
    assert result.trigger.operator_context == "private context\nsecond line\n"
    assert "private context" not in repr(result)
    assert services.effects == []


def test_prepublication_checkpoint_needs_no_translation_pr(services):
    services.row["target_sha"] = None
    services.job["target_sha"] = None
    services.head = None
    result = admit(services)
    assert result.checkpoint.target_sha is None
    assert services.effects == []


@pytest.mark.parametrize("fault", ["pending", "generic", "mismatched-stage", "started"])
def test_admission_rejects_unacknowledged_or_non_semantic_handoff(services, fault):
    if fault == "pending":
        services.row["status"] = "pending"
    elif fault == "generic":
        services.job["error"] = "prepare_failed"
    elif fault == "mismatched-stage":
        services.job["error"] = "continuable_translation"
    else:
        services.job["status"] = "started"
    with pytest.raises(PersistenceError):
        admit(services)
    assert services.effects == []


@pytest.mark.parametrize(
    "fault",
    [
        "label_actor",
        "comment_author",
        "missing_command",
        "empty_command",
        "same_time",
        "later_command",
        "malformed_event",
        "empty_allowlist",
        "expired",
        "closed",
        "stale_head",
        "missing_head",
        "source_identity",
        "source_sha",
        "missing_provenance",
        "duplicate_provenance",
        "partial_provenance",
        "wrong_branch",
        "foreign_head",
        "foreign_base",
        "wrong_pr_number",
    ],
)
def test_rejection_is_read_only_and_never_echoes_operator_context(services, fault, capsys):
    pr = 52
    allowed = "writer"
    if fault == "label_actor":
        services.events[0]["actor"]["login"] = "outsider"
    elif fault == "comment_author":
        services.comments[0]["user"]["login"] = "outsider"
    elif fault == "missing_command":
        services.comments = []
    elif fault == "empty_command":
        services.comments[0]["body"] = "/ydbdoc continue\n\t"
    elif fault in {"same_time", "later_command"}:
        services.comments[0]["created_at"] = (
            "2026-09-22T11:00:00Z" if fault == "same_time" else "2026-09-22T12:00:00Z"
        )
        services.comments[0]["updated_at"] = services.comments[0]["created_at"]
    elif fault == "malformed_event":
        services.events[0]["created_at"] = "private context"
    elif fault == "empty_allowlist":
        allowed = ""
    elif fault == "expired":
        services.row["created_at"] = NOW - timedelta(days=14)
    elif fault == "closed":
        services.row["status"] = "closed"
    elif fault == "stale_head":
        services.head = "d" * 40
    elif fault == "missing_head":
        services.head = None
    elif fault == "source_identity":
        services.row["source_pr"] = 43
    elif fault == "source_sha":
        services.row["source_sha"] = "d" * 40
    elif fault == "missing_provenance":
        services.prs[52]["body"] = ""
    elif fault == "duplicate_provenance":
        services.prs[52]["body"] *= 2
    elif fault == "partial_provenance":
        services.prs[52]["body"] = "<!-- ydbdoc-source-pr:42 -->"
    elif fault == "wrong_branch":
        services.prs[52]["head"]["ref"] = "translation/pr-42"
    elif fault == "foreign_head":
        services.prs[52]["head"]["repo"] = {"full_name": "outsider/ydb"}
    elif fault == "foreign_base":
        services.prs[52]["base"]["repo"] = {"full_name": "outsider/ydb"}
    elif fault == "wrong_pr_number":
        services.prs[52]["number"] = 53
    with pytest.raises((RuntimeBoundaryError, PersistenceError)) as error:
        admit(services, pr, allowed)
    assert services.effects == []
    assert "private context" not in str(error.value)
    assert "private context" not in capsys.readouterr().out


def test_missing_provenance_cannot_reclassify_an_associated_translation_as_source(services):
    services.row["trigger_pr"] = 52
    services.prs[52]["body"] = ""
    with pytest.raises(RuntimeBoundaryError):
        admit(services, 52)
    assert services.effects == []


def test_multiple_open_checkpoints_fail_closed_through_real_store(services, monkeypatch):
    def execute(statement, parameters, /):
        return [services.row, {**services.row, "continuation_id": "checkpoint-2"}]

    monkeypatch.setattr(services, "execute", execute)
    with pytest.raises(PersistenceError, match="ambiguous"):
        admit(services)
    assert services.effects == []


def test_actual_actors_are_authorized_even_when_actions_actor_is_different(services):
    source = RuntimeSource(
        {"YDBDOC_ALLOWED_ACTORS": "other, writer\nthird", "GITHUB_TRIGGERING_ACTOR": "outsider"},
        GitHubBackend(services.github),
    )
    result = source.authorize_continue(42, YdbPersistence(services), now=NOW)
    assert result.trigger.label_actor == "writer"
    assert result.trigger.comment_author == "writer"


def test_source_fork_is_not_mistaken_for_untrusted_translation_provenance(services):
    services.prs[42]["head"]["repo"] = {"full_name": "contributor/ydb"}
    assert admit(services).source_pr == 42


def test_authorization_failure_stops_before_checkpoint_and_pr_reads(services):
    services.events[0]["actor"]["login"] = "outsider"
    with pytest.raises(RuntimeBoundaryError, match="actor_not_authorized"):
        admit(services)
    assert services.calls == [("GET", "/repos/ydb-platform/ydb/issues/42/events?per_page=100")]


def test_ordinary_comment_cannot_become_command_after_label(services):
    services.comments[0]["body"] = "ordinary discussion"
    with pytest.raises(RuntimeBoundaryError, match="continue_command_missing"):
        admit(services)
    services.comments[0].update(
        body="/ydbdoc continue\nprivate context introduced after label",
        updated_at="2026-09-22T11:01:00Z",
    )
    with pytest.raises(RuntimeBoundaryError, match="continue_command_edited_after_label"):
        admit(services)
    assert services.effects == []


@pytest.mark.parametrize("updated_at", ["2026-09-22T11:00:00Z", "2026-09-22T11:01:00Z"])
def test_selected_command_edited_at_or_after_label_rejects_without_fallback(services, updated_at):
    # This older, fully valid command must not hide the ineligible selected body.
    services.comments.append(
        {
            "id": 3,
            "user": {"login": "writer"},
            "created_at": "2026-09-22T09:00:00Z",
            "updated_at": "2026-09-22T09:00:00Z",
            "body": "/ydbdoc continue\nolder context",
        }
    )
    assert admit(services).trigger.comment_id == 2
    services.comments[0].update(
        body="/ydbdoc continue\nprivate replacement context", updated_at=updated_at
    )
    with pytest.raises(RuntimeBoundaryError, match="continue_command_edited_after_label") as error:
        admit(services)
    assert "private" not in str(error.value)
    assert services.effects == []


def test_prelabel_edit_is_retained_and_selection_still_uses_creation_time(services):
    services.comments[0]["updated_at"] = "2026-09-22T10:30:00Z"
    services.comments.append(
        {
            "id": 3,
            "user": {"login": "writer"},
            "created_at": "2026-09-22T09:00:00Z",
            "updated_at": "2026-09-22T10:45:00Z",
            "body": "/ydbdoc continue\nolder context edited more recently",
        }
    )
    decoded = GitHubBackend(services.github).read_operator_comments(42)
    assert decoded[0].updated_at == datetime(2026, 9, 22, 10, 30, tzinfo=UTC)
    result = admit(services)
    assert result.trigger.comment_id == 2
    assert result.trigger.operator_context == "private context\nsecond line\n"
    assert services.effects == []


@pytest.mark.parametrize(
    "updated_at",
    [
        None,
        "missing",
        "private malformed timestamp",
        "2026-09-22T10:30:00",
        "2026-09-22T11:30:00+00:60",
        "2026-09-22T09:59:59Z",
    ],
)
def test_invalid_comment_update_timestamp_fails_closed(services, updated_at, capsys):
    assert admit(services).trigger.comment_id == 2
    if updated_at == "missing":
        del services.comments[0]["updated_at"]
    else:
        services.comments[0]["updated_at"] = updated_at
    with pytest.raises(RuntimeBoundaryError, match="operator_comments_invalid") as error:
        admit(services)
    assert services.effects == []
    assert "private" not in str(error.value)
    captured = capsys.readouterr()
    assert "private" not in captured.out + captured.err
