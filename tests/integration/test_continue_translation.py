"""Public continuation with real replay/assembly/persistence and only remote I/O fakes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _runtime_services import raw_translation_source
from test_checkpoint_capture import CaptureServices

from ydbdoc_review_ng import application
from ydbdoc_review_ng.continuation import ContinuationStage
from ydbdoc_review_ng.models import HttpResponse
from ydbdoc_review_ng.persistence import PersistenceError
from ydbdoc_review_ng.quality import Verdict

RU = "ydb/docs/ru/core/"
EN = "ydb/docs/en/core/"
CONTEXT = "Private operator guidance\nUse the frozen Russian source.\nhttps://operator.test/context-only\n"


class ContinueServices(CaptureServices):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.continuing = False
        self.direction_values = None
        self.invalid_pending = None
        self.prompts = []
        self.operations = []
        self.reads = []
        self.parents = []
        self.commands = [
            {
                "id": 99,
                "user": {"login": "maintainer"},
                "created_at": "2026-09-21T10:00:00Z",
                "updated_at": "2026-09-21T10:00:00Z",
                "body": "/ydbdoc continue\n" + CONTEXT,
            }
        ]

    def execute(self, statement, parameters):
        self.operations.append((statement, dict(parameters)))
        return super().execute(statement, parameters)

    def github(self, method, path, payload):
        relative = path.removeprefix("/repos/ydb-platform/ydb")
        if relative.endswith("/events?per_page=100"):
            self.events.append((method, path))
            return [
                {
                    "id": 100,
                    "event": "labeled",
                    "label": {"name": "doc_continue"},
                    "actor": {"login": "maintainer"},
                    "created_at": "2026-09-21T11:00:00Z",
                }
            ]
        if self.continuing and method == "GET" and relative.endswith("/comments?per_page=100"):
            self.events.append((method, path))
            # Operator commands are distinct from the publisher's current QA comment.
            return self.commands + [
                {
                    **comment,
                    "created_at": "2026-09-21T12:00:00Z",
                    "updated_at": "2026-09-21T12:00:00Z",
                }
                for comment in self.comments
                if "user" in comment
            ]
        if self.continuing and relative.startswith("/contents/"):
            self.reads.append(relative)
        if relative == "/git/commits" and method == "POST":
            self.parents.append(payload["parents"])
        result = super().github(method, path, payload)
        if relative == "/pulls/43":
            result["number"] = 43
        return result

    def model(self, request):
        body = json.loads(request.body)
        prompt = body["messages"][-1]["text"]
        response = super().model(request)
        self.prompts.append((self.roles[-1], prompt))
        values = None
        if self.continuing and self.roles[-1] == "direction" and self.direction_values is not None:
            values = self.direction_values
        if self.continuing and self.roles[-1] == "translate":
            source = raw_translation_source(prompt)
            raw = source.replace("Source", "Resumed")
            if self.invalid_pending and self.invalid_pending in source:
                raw = "invalid response"
            data = json.loads(response.body)
            data["result"]["alternatives"][0]["message"]["text"] = raw
            return HttpResponse(200, json.dumps(data).encode(), Decimal("0.01"))
        if values is not None:
            data = json.loads(response.body)
            data["result"]["alternatives"][0]["message"]["text"] = json.dumps(values)
            response = HttpResponse(200, json.dumps(data).encode(), Decimal("0.01"))
        return response

    def stop_and_continue(self):
        with pytest.raises(application.WorkflowError):
            self.translate()
        saved = self.checkpoint()
        self.continuing = True
        self.stop = None
        self.roles.clear()
        self.prompts.clear()
        self.events.clear()
        self.operations.clear()
        self.critics = 0
        return saved

    def resume(self, pr=42):
        request = getattr(application, "ContinueWorkflowInput", None)
        assert request is not None, "public ContinueWorkflowInput is required"
        runtime = self.runtime()
        assert hasattr(runtime, "doc_continue"), "public runtime doc_continue is required"
        return runtime.doc_continue(request(pr))


def test_pending_only_preserves_accepted_source_fragments_and_records_current_costs(capsys):
    services = ContinueServices(names=("a", "b"), stop="translation")
    protected = b"\n```sql\nSELECT 1;\n```\n"
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + "a.md"] += protected
        tree[EN + "a.md"] = b"# Poison old target\n```sql\nDROP TABLE t;\n```\n"
    saved = services.stop_and_continue()
    accepted = services.rows[saved.continuation_id]["state"]
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.roles == ["translate", "critic", "critic"]
    assert services.files[EN + "a.md"] == b"# Translated\n" + protected
    assert services.files[EN + "b.md"] == b"# Resumed b\n"
    assert services.parents == [[services.base]]
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.rows[saved.continuation_id]["state"] == accepted
    assert not any("SUM" in statement for statement, _ in services.operations)
    attempts = [params for _, params in services.operations if "attempt_id" in params]
    assert len(attempts) == 3
    assert {params["job_id"] for params in attempts} == {result.job_id}
    assert sum(params["cost_rub"] for params in attempts) == Decimal("0.03")
    assert services.jobs[result.job_id]["source_sha"] == saved.source_sha.value
    assert services.jobs[result.job_id]["mode"] == "doc_continue"
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert services.prompts[0][0] == "translate" and CONTEXT in services.prompts[0][1]
    assert all(CONTEXT not in prompt for role, prompt in services.prompts if role == "critic")
    assert all(CONTEXT not in comment["body"] for comment in services.comments)
    captured = capsys.readouterr()
    assert CONTEXT not in captured.out + captured.err
    roles = list(services.roles)
    with pytest.raises(application.WorkflowError, match="authorize"):
        services.resume()
    assert services.roles == roles
    assert any(
        "continuations`" in statement and "pr_number" in parameters
        for statement, parameters in services.operations
    )


def test_translation_pr_uses_saved_head_after_source_base_and_inventory_move():
    class MovedSource(ContinueServices):
        def github(self, method, path, payload):
            relative = path.removeprefix("/repos/ydb-platform/ydb")
            if self.continuing:
                assert not relative.endswith("/heads/main"), "current base is not authoritative"
                assert "/files?" not in relative, "checkpoint owns the source inventory"
            result = super().github(method, path, payload)
            if self.continuing and relative == "/pulls/42":
                result["head"]["sha"] = "f" * 40
                result["changed_files"] = 99
            return result

    services = MovedSource(names=("a", "b"), stop="translation")
    services.branch_head = services.translated
    services.snapshots[services.translated] = {
        **services.files,
        EN + "a.md": b"# Poison accepted target\n",
        EN + "b.md": b"# Poison pending target\n",
    }
    services.pr_exists = True
    saved = services.stop_and_continue()
    result = services.resume(43)
    assert result.verdict is Verdict.GREEN
    assert services.parents == [[saved.target_sha.value]]
    assert services.roles == ["translate", "critic", "critic"]
    assert services.files[EN + "a.md"] == b"# Translated\n"
    assert services.files[EN + "b.md"] == b"# Resumed b\n"
    assert all("ref=" + services.source in path for path in services.reads if "/ru/core/" in path)
    assert services.jobs[result.job_id]["pr_number"] == 43
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert sum("<!-- ydbdoc-current-qa -->" in c["body"] for c in services.comments) == 1


def test_public_continue_restores_protected_link_delete_rename_and_pinned_metadata():
    services = ContinueServices(names=("a", "b"), stop="translation")
    services.changes[1]["status"] = "added"
    services.changes += [
        {"status": "removed", "filename": RU + "deleted.md"},
        {
            "status": "renamed",
            "filename": RU + "moved.md",
            "previous_filename": RU + "old.md",
            "changes": 0,
        },
    ]
    source_b = b"# Source b\n\n[Source link](https://source.test/exact#anchor)\n"
    for tree in [services.files, *services.snapshots.values()]:
        tree.update(
            {
                RU + "b.md": source_b,
                RU + "moved.md": b"# Original name\n",
                EN + "old.md": b"# Whole pinned translation\n",
                EN + "deleted.md": b"# Deleted counterpart\n",
                RU + "toc.yaml": b"items:\n  - name: New\n    href: b.md\n",
                EN + "toc.yaml": b"items:\n  - name: Old\n    href: old.md\n",
            }
        )
    services.branch_head = services.translated
    services.snapshots[services.translated] = dict(services.files)
    services.pr_exists = True
    saved = services.stop_and_continue()
    services.snapshots[services.translated][EN + "toc.yaml"] = b"POISON: ["
    services.snapshots[services.translated][EN + "old.md"] = b"# Wrong current counterpart\n"
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.roles == ["translate", "critic", "critic", "critic"]
    assert services.files[EN + "b.md"] == source_b.replace(b"Source", b"Resumed")
    assert services.files[EN + "moved.md"] == b"# Whole pinned translation\n"
    assert EN + "old.md" not in services.files and EN + "deleted.md" not in services.files
    assert b"href: b.md" in services.files[EN + "toc.yaml"]
    assert b"href: moved.md" in services.files[EN + "toc.yaml"]
    assert b"POISON" not in services.files[EN + "toc.yaml"]
    assert (
        services.files["ydb/docs/en/redirects.yaml"]
        == b'redirects:\n  - from: "core/old.md"\n    to: "core/moved.md"\n'
    )
    assert services.rows[saved.continuation_id]["status"] == "closed"


def test_green_closes_only_loaded_row_after_successful_audit():
    class CheckClose(ContinueServices):
        def execute(self, statement, parameters):
            if self.continuing and "SET status = 'closed'" in statement:
                assert list(self.jobs.values())[-1]["status"] == "succeeded"
            return super().execute(statement, parameters)

    services = CheckClose(stop="translation")
    saved = services.stop_and_continue()
    services.rows["closed-history"] = {
        **services.rows[saved.continuation_id],
        "continuation_id": "closed-history",
        "status": "closed",
    }
    services.resume()
    closes = [
        params["continuation_id"]
        for statement, params in services.operations
        if "SET status = 'closed'" in statement
    ]
    assert closes == [saved.continuation_id]


def test_repeated_stop_keeps_exact_saved_head_if_branch_moves_during_model():
    class MovedBranch(ContinueServices):
        def model(self, request):
            result = super().model(request)
            if self.continuing:
                self.branch_head = "f" * 40
            return result

    services = MovedBranch(stop="translation")
    saved = services.stop_and_continue()
    services.invalid_pending = "Source b"
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert set(services.rows) == {saved.continuation_id}
    assert list(services.jobs.values())[-1]["error"] != "continuable_translation"


def test_valid_response_cannot_publish_after_saved_branch_moves():
    class MovedBranch(ContinueServices):
        def model(self, request):
            result = super().model(request)
            if self.continuing:
                self.branch_head = "f" * 40
                self.snapshots[self.branch_head] = dict(self.files)
            return result

    services = MovedBranch(names=("a", "b"), stop="translation")
    services.stop_and_continue()
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.commits == 0 and services.blobs == {}
    assert not any(method in {"POST", "PATCH"} for method, _ in services.events)


def test_noop_with_existing_branch_cannot_close_checkpoint_after_branch_disappears():
    class DeletedBranch(ContinueServices):
        def model(self, request):
            result = super().model(request)
            if self.continuing and self.roles[-1] == "critic":
                self.branch_head = None
            return result

    services = DeletedBranch(names=("a", "b"), stop="translation")
    services.branch_head = services.translated
    services.snapshots[services.translated] = {
        **services.files,
        EN + "a.md": b"# Translated\n",
        EN + "b.md": b"# Resumed b\n",
    }
    services.pr_exists = True
    saved = services.stop_and_continue()
    with pytest.raises(application.WorkflowError, match="report"):
        services.resume()
    assert services.commits == 0
    assert services.rows[saved.continuation_id]["status"] == "open"
    assert list(services.jobs.values())[-1]["status"] == "failed"


def test_direction_retry_alone_hands_off_new_job_without_extending_expiry():
    services = ContinueServices(stop="direction")
    saved = services.stop_and_continue()
    services.rows[saved.continuation_id]["created_at"] -= timedelta(days=5)
    saved = services.checkpoint()
    with pytest.raises(application.WorkflowError):
        services.resume()
    following = services.checkpoint()
    assert services.roles == ["direction"]
    assert CONTEXT in services.prompts[0][1]
    assert following.state.stage is ContinuationStage.DIRECTION
    assert following.continuation_id != saved.continuation_id
    assert following.job_id == following.continuation_id
    assert services.jobs[following.job_id]["mode"] == "doc_continue"
    assert services.jobs[following.job_id]["error"] == "continuable_direction"
    assert following.created_at == saved.created_at and following.expires_at == saved.expires_at
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert sum(row["status"] == "open" for row in services.rows.values()) == 1
    assert services.commits == 0


def test_direction_selection_excludes_complete_pair_before_translation():
    services = ContinueServices(names=("a", "b"), stop="direction")
    saved = services.stop_and_continue()
    services.direction_values = {"a.md": "complete_pair", "b.md": "ru_to_en"}
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.roles == ["direction", "translate", "critic"]
    assert services.files[EN + "a.md"] == b"# Old a\n"
    assert services.files[EN + "b.md"] == b"# Resumed b\n"
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert all(CONTEXT in prompt for role, prompt in services.prompts if role != "critic")


def test_all_complete_direction_finishes_noop_without_pr_or_other_models():
    services = ContinueServices(names=("a",), stop="direction")
    saved = services.stop_and_continue()
    services.direction_values = {"a.md": "complete_pair"}
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.roles == ["direction"]
    assert services.commits == 0 and not services.pr_exists
    assert not any(method in {"POST", "PATCH"} for method, _ in services.events)
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert services.rows[saved.continuation_id]["status"] == "closed"


def test_repeated_pending_failure_preserves_documents_and_pending_order():
    services = ContinueServices(stop="translation")
    saved = services.stop_and_continue()
    services.invalid_pending = "Source c"
    with pytest.raises(application.WorkflowError):
        services.resume()
    following = services.checkpoint()
    assert services.roles == ["translate", "translate", "translate"]
    assert following.state.accepted_documents[0] == saved.state.accepted_documents[0]
    assert following.state.accepted_documents[1].translated_markdown == "# Resumed b\n"
    assert [path.value for path in following.state.pending_paths] == [EN + "c.md"]
    assert following.expires_at == saved.expires_at
    assert following.job_id != saved.job_id
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.commits == 0


def test_handoff_clock_failure_uses_new_audit_time_not_inherited_expiry(monkeypatch):
    services = ContinueServices(stop="translation")
    saved = services.stop_and_continue()
    services.rows[saved.continuation_id]["created_at"] -= timedelta(days=5)
    services.invalid_pending = "Source b"
    started_at = datetime.now(UTC)

    def now(_clock):
        if len(services.rows) > 1:
            raise OSError("clock unavailable after pending save")
        return started_at

    monkeypatch.setattr("ydbdoc_review_ng.runtime.SystemClock.now", now)
    with pytest.raises(application.WorkflowError):
        services.resume()
    job = list(services.jobs.values())[-1]
    assert job["finished_at"] >= job["started_at"] == started_at
    assert job["error"] == "terminal_audit_failed"


def test_pending_success_uses_full_review_single_repair_and_captures_red():
    services = ContinueServices(stop="translation")
    saved = services.stop_and_continue()
    services.stop = "review"
    result = services.resume()
    following = services.checkpoint()
    assert result.verdict is Verdict.RED and result.repair_applied
    assert services.roles == [
        "translate",
        "translate",
        "critic",
        "repair",
        "critic",
        "critic",
        "critic",
    ]
    assert following.state.stage is ContinuationStage.REVIEW
    assert following.target_sha == result.final_commit_sha
    assert [p.value for p in following.state.review_paths] == [EN + "b.md"]
    assert following.job_id == result.job_id
    assert following.created_at == saved.created_at
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.comments[-1]["body"].startswith("RED\n")
    assert services.commits == 2


@pytest.mark.parametrize(
    "fault", ["missing_comment", "expired", "closed", "ambiguous", "digest", "stale", "job_marker"]
)
def test_invalid_checkpoint_stops_public_workflow_before_models_or_mutations(fault):
    services = ContinueServices(stop="translation")
    saved = services.stop_and_continue()
    row = services.rows[saved.continuation_id]
    if fault == "missing_comment":
        services.commands = []
    elif fault == "expired":
        row["created_at"] = datetime.now(UTC) - timedelta(days=14)
    elif fault == "closed":
        row["status"] = "closed"
    elif fault == "ambiguous":
        services.rows["other"] = {**row, "continuation_id": "other"}
    elif fault == "digest":
        state = json.loads(row["state"])
        state["scope_sha256"] = "0" * 64
        row["state"] = json.dumps(state).encode()
    elif fault == "stale":
        services.branch_head = "f" * 40
    else:
        services.jobs[saved.job_id]["error"] = "continuable_direction"
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == []
    assert not any(method in {"POST", "PATCH"} for method, _ in services.events)
    assert services.audit[-1]["status"] == "failed"


@pytest.mark.parametrize("failure", ["translate", "critic", "publish", "report", "attempt"])
def test_infrastructure_failure_does_not_create_another_checkpoint(failure):
    services = ContinueServices(stop="translation")
    saved = services.stop_and_continue()
    services.failure = failure
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert set(services.rows) == {saved.continuation_id}
    assert services.audit[-1]["status"] == "failed"


@pytest.mark.parametrize("fault", ["save", "terminal", "close", "activate"])
def test_replacement_handoff_failure_never_leaves_two_open_rows(fault):
    class FailedHandoff(ContinueServices):
        def execute(self, statement, parameters):
            if self.continuing:
                if fault == "save" and "/continuations`" in statement and "UPSERT" in statement:
                    raise OSError("checkpoint save unavailable")
                if fault == "terminal" and parameters.get("error") == "continuable_translation":
                    raise OSError("terminal unavailable")
                if fault == "close" and "SET status = 'closed'" in statement:
                    raise OSError("close unavailable")
                if "SET status = 'open'" in statement:
                    assert self.rows[self.original]["status"] == "open"
                    assert self.jobs[parameters["job_id"]]["error"] == "continuable_translation"
                    if fault == "activate":
                        raise OSError("activation unavailable")
            return super().execute(statement, parameters)

    services = FailedHandoff(stop="translation")
    saved = services.stop_and_continue()
    services.original = saved.continuation_id
    services.invalid_pending = "Source b"
    with pytest.raises(application.WorkflowError):
        services.resume()
    eligible = services.checkpoint()
    assert eligible.expires_at == saved.expires_at
    assert eligible.continuation_id == (
        list(services.jobs)[-1] if fault == "close" else saved.continuation_id
    )


class LifecycleServices(ContinueServices):
    """Apply each remote write before/after faults without faking lifecycle policy."""

    fault = None
    activated = False

    def execute(self, statement, parameters):
        if not self.continuing:
            return super().execute(statement, parameters)
        closing_old = (
            "SET status = 'closed'" in statement and parameters["continuation_id"] == self.original
        )
        activating = "SET status = 'open'" in statement
        successful = (
            parameters.get("status") == "succeeded"
            and "finished_at" in parameters
            and "role" not in parameters
        )
        if closing_old and self.fault == "close_before":
            raise OSError("old close unavailable before apply")
        if activating and self.fault == "activate_before":
            raise OSError("activation unavailable before apply")
        if successful and self.fault == "success_before":
            raise OSError("success unavailable before apply")
        if (
            self.fault == "activation_readback"
            and self.activated
            and "/continuations`" in statement
            and "SELECT" in statement
        ):
            raise OSError("activation readback unavailable")
        if (
            self.fault == "success_readback"
            and "/jobs`" in statement
            and "SELECT" in statement
            and self.jobs.get(parameters["job_id"], {}).get("status") == "succeeded"
        ):
            raise OSError("success readback unavailable")
        result = super().execute(statement, parameters)
        if activating:
            self.activated = True
            if self.fault == "activate_after":
                raise OSError("activation committed but acknowledgement lost")
        if closing_old and self.fault == "close_after":
            raise OSError("old close committed but acknowledgement lost")
        if successful and self.fault in {"success_after", "success_readback"}:
            raise OSError("success committed but acknowledgement lost")
        return result


@pytest.mark.parametrize("kind", ["direction", "translation"])
@pytest.mark.parametrize("fault", [None, "close_before", "close_after", "success_after"])
def test_green_consumption_prevents_paid_replay_despite_lost_acknowledgements(kind, fault):
    services = LifecycleServices(names=("a",) if kind == "direction" else ("a", "b"), stop=kind)
    if kind == "translation":
        services.branch_head = services.translated
        services.pr_exists = True
        services.snapshots[services.translated] = {
            **services.files,
            EN + "a.md": b"# Translated\n",
            EN + "b.md": b"# Resumed b\n",
        }
    saved = services.stop_and_continue()
    services.original = saved.continuation_id
    services.direction_values = {"a.md": "complete_pair"}
    services.fault = fault
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert services.rows[saved.continuation_id]["consumed_by_job_id"] == result.job_id
    assert services.commits == 0
    calls = ["direction"] if kind == "direction" else ["translate", "critic", "critic"]
    assert services.roles == calls
    with pytest.raises(PersistenceError):
        services.checkpoint()
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == calls
    assert services.jobs[result.job_id]["status"] == "succeeded"


def test_uncertain_success_readback_preserves_durable_success_and_blocks_repeat():
    services = LifecycleServices(names=("a",), stop="direction")
    saved = services.stop_and_continue()
    services.original = saved.continuation_id
    services.direction_values = {"a.md": "complete_pair"}
    services.fault = "success_readback"
    with pytest.raises(application.WorkflowError):
        services.resume()
    finished = list(services.jobs.values())[-1]
    assert finished["status"] == "succeeded"
    services.fault = None
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == ["direction"]


@pytest.mark.parametrize("ttl_cleanup", [False, True])
def test_fresh_public_continue_ignores_expired_completed_history(ttl_cleanup):
    services = ContinueServices(names=("a",), stop="direction")
    old = services.stop_and_continue()
    services.direction_values = {"a.md": "complete_pair"}
    completed = services.resume()
    assert completed.verdict is Verdict.GREEN
    history = services.rows[old.continuation_id]
    assert history["status"] == "closed"
    assert history["consumed_by_job_id"] == completed.job_id
    history["created_at"] -= timedelta(days=15)

    services.continuing = False
    services.stop = "direction"
    with pytest.raises(application.WorkflowError):
        services.translate()
    fresh = list(services.rows)[-1]
    assert fresh != old.continuation_id
    assert services.rows[fresh]["status"] == "open"
    services.continuing = True
    services.stop = None
    before = list(services.roles)
    if ttl_cleanup:
        services.rows.pop(old.continuation_id)

    result = services.resume()

    assert result.verdict is Verdict.GREEN
    assert services.rows[fresh]["status"] == "closed"
    assert services.rows[fresh]["consumed_by_job_id"] == result.job_id
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert services.roles == before + ["direction"]
    assert services.commits == 0 and not services.pr_exists
    with pytest.raises(application.WorkflowError, match="authorize"):
        services.resume()
    assert services.roles == before + ["direction"]


def test_confirmed_terminal_write_failure_retains_old_checkpoint_with_failed_audit():
    services = LifecycleServices(names=("a",), stop="direction")
    saved = services.stop_and_continue()
    services.original = saved.continuation_id
    services.direction_values = {"a.md": "complete_pair"}
    services.fault = "success_before"
    with pytest.raises(application.WorkflowError):
        services.resume()
    finished = list(services.jobs.values())[-1]
    assert finished["status"] == "failed"
    assert finished["error"] == "terminal_audit_failed"
    assert services.checkpoint().continuation_id == saved.continuation_id
    assert services.roles == ["direction"]


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "activate_before",
        "activate_after",
        "activation_readback",
        "close_before",
        "close_after",
    ],
)
def test_replacement_preserves_exactly_one_logical_checkpoint_after_boundary_failure(fault):
    services = LifecycleServices(stop="translation")
    saved = services.stop_and_continue()
    services.original = saved.continuation_id
    services.invalid_pending = "Source b"
    services.fault = fault
    with pytest.raises(application.WorkflowError):
        services.resume()
    consuming_job = list(services.jobs)[-1]
    services.fault = None
    eligible = services.checkpoint()
    assert eligible.continuation_id == (
        saved.continuation_id if fault == "activate_before" else consuming_job
    )
    assert eligible.created_at == saved.created_at
    assert eligible.expires_at == saved.expires_at
    assert eligible.state.accepted_documents == saved.state.accepted_documents
    assert services.roles == ["translate", "translate"] and services.commits == 0
    assert services.rows[saved.continuation_id]["consumed_by_job_id"] == consuming_job
    if fault == "activate_before":
        assert services.rows[consuming_job]["status"] == "pending"
    else:
        assert services.rows[consuming_job]["status"] == "open"
    services.invalid_pending = None
    assert services.resume().verdict is Verdict.GREEN
    paid_calls = list(services.roles)
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == paid_calls
    with pytest.raises(PersistenceError):
        services.checkpoint()
