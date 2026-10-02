"""Public continuation with real replay/assembly/persistence and only remote I/O fakes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _runtime_services import (
    classification_response,
    raw_translation_source,
    replace_response_text,
    request_prompt,
    request_schema,
    translation_segments,
)
from test_checkpoint_capture import CaptureServices

from ydbdoc_review_ng import application
from ydbdoc_review_ng.application import TranslateWorkflowInput
from ydbdoc_review_ng.continuation import ContinuationStage
from ydbdoc_review_ng.domain import GitSha
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
            # Operator commands authorize continue; keep them on the trigger PR only.
            # Never mix translation-PR QA into source-PR listings (§7 reporting).
            if "/issues/42/" in relative:
                rows = self.source_comments
            elif "/issues/43/" in relative:
                rows = self.comments
            else:
                rows = []
            stamped = [
                {
                    **comment,
                    "created_at": "2026-09-21T12:00:00Z",
                    "updated_at": "2026-09-21T12:00:00Z",
                }
                for comment in rows
                if "user" in comment
            ]
            return self.commands + stamped
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
        prompt = request_prompt(body)
        response = super().model(request)
        self.prompts.append((self.roles[-1], prompt))
        values = None
        if self.continuing and self.roles[-1] == "direction" and self.direction_values is not None:
            values = classification_response(prompt, decisions=self.direction_values)
        if (
            self.continuing
            and self.roles[-1] == "translate"
            and "<TRANSLATION_DRAFT_" not in prompt
        ):
            schema_wrapper = request_schema(body)
            if schema_wrapper is not None and "strings" in schema_wrapper["schema"]["properties"]:
                return response
            source = raw_translation_source(prompt)
            if schema_wrapper is not None and all(
                key.startswith("segment_") for key in schema_wrapper["schema"]["properties"]
            ):
                values = translation_segments(prompt, "Resumed", preserve_suffix=True)
                values = {key: value.replace("Source", "Resumed") for key, value in values.items()}
                raw = json.dumps(values)
            else:
                raw = source.replace("Source", "Resumed")
            if self.invalid_pending and self.invalid_pending in source:
                raw = "{}"
            return HttpResponse(200, replace_response_text(response.body, raw), Decimal("0.01"))
        if values is not None:
            response = HttpResponse(
                200,
                replace_response_text(response.body, json.dumps(values)),
                Decimal("0.01"),
            )
        return response

    def runtime(self):
        runtime = super().runtime()
        content = runtime._workflows._content
        # Soft-publish no longer opens translation checkpoints. Continue coverage
        # still seeds pending_paths via this harness-only switch, including when
        # a resume intentionally re-fails pending documents.
        if self.stop == "translation" or (
            self.continuing and self.invalid_pending is not None
        ):
            content._legacy_pending_translation_stop = True
        return runtime

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
    assert services.roles[:1] == ["translate"] and services.roles[-2:] == ["critic", "arbiter"]
    assert services.files[EN + "a.md"].endswith(protected)
    assert b"```sql\nSELECT 1;\n```" in services.files[EN + "a.md"]
    assert services.files[EN + "b.md"] == b"# Resumed b\n"
    assert services.parents == [[services.base]]
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.rows[saved.continuation_id]["state"] == accepted
    assert not any("SUM" in statement for statement, _ in services.operations)
    attempts = [params for _, params in services.operations if "attempt_id" in params]
    assert len(attempts) >= 3
    assert {params["job_id"] for params in attempts} == {result.job_id}
    assert services.jobs[result.job_id]["source_sha"] == saved.source_sha.value
    assert services.jobs[result.job_id]["mode"] == "doc_continue"
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert services.prompts[0][0] == "translate" and CONTEXT in services.prompts[0][1]
    assert all(CONTEXT in prompt for _, prompt in services.prompts)
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


def test_continue_uses_saved_inventory_after_source_head_and_inventory_move():
    """§5.3: continue freezes source inventory/SHA; live source PR head is ignored.

    New ``doc_translate`` deletes the translation branch before work (§5.1), so a
    harness translation-stage stop has ``target_sha=null`` and is continued from
    the source PR. Translation-PR continue still requires a matching published head.
    """

    class MovedSource(ContinueServices):
        def github(self, method, path, payload):
            relative = path.removeprefix("/repos/ydb-platform/ydb")
            if self.continuing:
                assert not relative.endswith("/heads/main"), "current base is not authoritative"
                assert "/files?" not in relative, "checkpoint owns the source inventory"
            result = super().github(method, path, payload)
            if self.continuing and relative == "/pulls/42":
                result = dict(result)
                result["head"] = dict(result["head"])
                result["head"]["sha"] = "f" * 40
                result["changed_files"] = 99
            return result

    services = MovedSource(names=("a", "b"), stop="translation")
    saved = services.stop_and_continue()
    assert saved.target_sha is None
    result = services.resume(42)
    assert result.verdict is Verdict.GREEN
    assert services.roles[:1] == ["translate"] and services.roles[-2:] == ["critic", "arbiter"]
    assert services.files[EN + "b.md"] == b"# Resumed b\n"
    assert all("ref=" + services.source in path for path in services.reads if "/ru/core/" in path)
    assert services.jobs[result.job_id]["pr_number"] == 42
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
    assert services.roles.count("translate") >= 1
    assert services.roles[-2:] == ["critic", "arbiter"]
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


def test_fresh_translate_deletes_previous_branch_and_closes_open_checkpoints():
    services = ContinueServices(names=("a", "b"), stop="direction")
    saved = services.stop_and_continue()
    assert services.rows[saved.continuation_id]["status"] == "open"
    services.branch_head = services.translated
    services.snapshots[services.translated] = dict(services.files)
    services.pr_exists = True
    services.stop = None
    services.continuing = False
    services.events.clear()
    # Fresh doc_translate must wipe recovery state (§5.1) before prepare/report.
    result = services.runtime().doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    assert result.verdict is Verdict.GREEN
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert any(
        method == "DELETE" and "/git/refs/heads/translation" in path
        for method, path in services.events
    )


def test_direction_retry_alone_hands_off_new_job_without_extending_expiry():
    services = ContinueServices(stop="direction")
    saved = services.stop_and_continue()
    services.stop = "direction"
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


def test_direction_selection_translates_all_markdown_pairs_for_selected_direction():
    """§1.1/§1.2: one selected direction; Python translates every Markdown pair.

    Fixture ``complete_pair`` labels only feed the direction-only JSON helper; they
    do not exclude individual pages once ``translation_required`` is true.
    """
    services = ContinueServices(names=("a", "b"), stop="direction")
    saved = services.stop_and_continue()
    services.direction_values = {"a.md": "complete_pair", "b.md": "ru_to_en"}
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.roles == ["direction", "translate", "translate", "critic", "arbiter"]
    assert services.files[EN + "a.md"] in {b"# Translated\n", b"# Resumed a\n"}
    assert services.files[EN + "b.md"] == b"# Resumed b\n"
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert all(
        CONTEXT in prompt
        for role, prompt in services.prompts
        if role not in {"critic", "arbiter"}
    )


def test_selected_direction_survives_translation_checkpoint_without_reclassification():
    """Saved direction continues pending translation without a second classifier call."""
    services = ContinueServices(names=("a", "b"), stop="direction")
    services.stop_and_continue()
    services.direction_values = {"a.md": "complete_pair", "b.md": "ru_to_en"}
    services.invalid_pending = "Source b"
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.checkpoint().state.stage is ContinuationStage.TRANSLATION
    services.invalid_pending = None
    services.roles.clear()
    services.resume()
    assert "direction" not in services.roles
    assert services.roles[:1] == ["translate"] and services.roles[-2:] == ["critic", "arbiter"]
    assert services.files[EN + "a.md"] in {b"# Translated\n", b"# Resumed a\n"}
    assert services.files[EN + "b.md"] == b"# Resumed b\n"


def test_toc_no_action_replays_exact_saved_decision_without_classifier():
    class Services(ContinueServices):
        def model(self, request):
            response = super().model(request)
            if self.roles[-1] == "direction":
                values = classification_response(
                    request_prompt(json.loads(request.body)),
                    decisions={"toc.yaml": "complete_pair"},
                )
                return HttpResponse(
                    200, replace_response_text(response.body, json.dumps(values)), Decimal("0.01")
                )
            return response

    services = Services(names=("a", "b"), stop="translation")
    services.changes.append({"status": "modified", "filename": RU + "toc.yaml"})
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + "toc.yaml"] = (
            b"items:\n- name: A\n  href: a.md\n- name: B\n  href: b.md\n"
        )
        tree[EN + "toc.yaml"] = b"items:\n- name: A\n  href: a.md\n"
    # Source TOC before the PR lacked b.md; after includes it as a byte-prefix append.
    services.snapshots[services.base][RU + "toc.yaml"] = b"items:\n- name: A\n  href: a.md\n"
    saved = services.stop_and_continue()
    wire = json.loads(services.rows[saved.continuation_id]["source_inventory"])
    assert wire["semantic_actions"][-1]["path"] == RU + "toc.yaml"
    assert wire["semantic_actions"][-1]["operation"] == "modify"
    assert wire["semantic_actions"][-1]["action"] == "toc_delta"
    assert services.resume().verdict is Verdict.GREEN
    assert services.roles[:1] == ["translate"] and services.roles[-2:] == ["critic", "arbiter"]
    assert b"href: b.md" in services.files[EN + "toc.yaml"]


@pytest.mark.parametrize(
    "corruption", ["legacy", "missing_actions", "unknown_path", "duplicate", "operation"]
)
def test_corrupt_frozen_inventory_fails_before_continuation_models(corruption):
    services = ContinueServices(names=("a", "b"), stop="translation")
    saved = services.stop_and_continue()
    row = services.rows[saved.continuation_id]
    wire = json.loads(row["source_inventory"])
    if corruption == "legacy":
        wire = wire["files"]
    elif corruption == "missing_actions":
        wire.pop("semantic_actions")
    elif corruption == "unknown_path":
        wire["semantic_actions"][0]["path"] = RU + "invented.md"
    elif corruption == "duplicate":
        wire["semantic_actions"][1] = wire["semantic_actions"][0]
    else:
        wire["semantic_actions"][0]["operation"] = "add"
    row["source_inventory"] = json.dumps(wire).encode()
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == [] and services.commits == 0
    assert not any(
        method in {"POST", "PATCH"} or (method == "DELETE" and "/labels/" not in path)
        for method, path in services.events
    )


@pytest.mark.parametrize("merged", [False, True])
def test_direction_continuation_keeps_original_diff_snapshots_after_pr_moves(merged):
    class Services(ContinueServices):
        def github(self, method, path, payload):
            result = super().github(method, path, payload)
            if self.continuing and path.endswith("/pulls/42"):
                result["base"]["sha"] = "d" * 40
                result["head"]["sha"] = "f" * 40
                result["merged"] = merged
                result["merge_commit_sha"] = "e" * 40
            return result

    services = Services(names=("a",), stop="direction")
    services.stop_and_continue()
    services.direction_values = {"a.md": "complete_pair"}
    services.resume()
    prompt = services.prompts[0][1]
    data = json.loads(prompt.split("\nInventory: ", 1)[1].split("\n\n", 1)[0])
    assert data["before_sha"] == "b" * 40
    assert data["after_sha"] == "a" * 40
    assert services.roles == ["direction"]


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
    assert services.roles.count("translate") >= 3
    assert following.state.target_sha is None
    assert [path.value for path in following.state.pending_paths] == [
        EN + "a.md",
        EN + "b.md",
        EN + "c.md",
    ]
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
    assert services.roles.count("translate") >= 2
    assert services.roles[-2:] == ["critic", "arbiter"]
    assert following.state.stage is ContinuationStage.REVIEW
    assert following.target_sha == result.final_commit_sha
    assert [p.value for p in following.state.review_paths] == [EN + "b.md"]
    assert following.job_id == result.job_id
    assert following.created_at == saved.created_at
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.comments[-1]["body"].startswith("🔴 RED\n")
    # Critic pushes the successful chunk immediately; workflow may commit again.
    assert services.commits >= 1


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
    if kind == "direction":
        assert services.commits == 0
        calls = ["direction"]
    else:
        assert services.roles.count("translate") >= 1
        assert services.roles[-2:] == ["critic", "arbiter"]
        calls = list(services.roles)
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
    assert eligible.state.pending_paths == saved.state.pending_paths
    assert eligible.state.target_sha == saved.state.target_sha
    assert services.roles.count("translate") >= 2 and services.commits == 0
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


def test_continue_review_allows_missing_soft_published_target() -> None:
    """REQUIREMENTS §5.3: REVIEW continue keeps null soft-publish targets (#1)."""

    class FailB(ContinueServices):
        def model(self, request):
            body = json.loads(request.body)
            props = request_schema(body)["schema"]["properties"]
            role = (
                "direction"
                if "translation_required" in props
                else "critic"
                if "files" in props
                else "arbiter"
                if "findings" in props
                else "toc"
                if "strings" in props
                else "translate"
            )
            prompt = request_prompt(body)
            if not self.continuing and (
                role == "critic" or (role == "translate" and "Source b" in prompt)
            ):
                self.roles.append(role)
                return HttpResponse(503, b"{}", None)
            return super().model(request)

    services = FailB(names=("a", "b"), stop="review")
    for tree in [services.files, *services.snapshots.values()]:
        tree.pop(EN + "b.md", None)

    result = services.translate()
    assert result.verdict is Verdict.RED
    assert services.commits >= 1
    assert any(row["status"] == "open" for row in services.rows.values())

    services.continuing = True
    services.stop = None
    services.roles.clear()
    resumed = services.resume()
    assert resumed.verdict in {Verdict.GREEN, Verdict.YELLOW, Verdict.RED}
    assert services.roles  # models ran; snapshot admission succeeded


def test_zero_commit_null_toc_keeps_review_checkpoint() -> None:
    """REQUIREMENTS §4.2: TOC=null RED still opens target_sha=null checkpoint (#6)."""

    class NullToc(ContinueServices):
        def model(self, request):
            body = json.loads(request.body)
            props = request_schema(body)["schema"]["properties"]
            role = (
                "direction"
                if "translation_required" in props
                else "critic"
                if "files" in props
                else "arbiter"
                if "findings" in props
                else "toc"
                if "strings" in props
                else "translate"
            )
            if role == "critic":
                self.roles.append(role)
                return HttpResponse(503, b"{}", None)
            response = super().model(request)
            if role == "toc":
                return HttpResponse(
                    200,
                    replace_response_text(response.body, json.dumps({"strings": {}})),
                    Decimal(".01"),
                )
            return response

    services = NullToc(names=(), stop="rename_red")
    services.changes = [{"status": "modified", "filename": RU + "toc.yaml"}]
    services.snapshots[services.base][RU + "toc.yaml"] = b"title: Old\nitems: []\n"
    services.snapshots[services.base][EN + "toc.yaml"] = b"title: Old EN\nitems: []\n"
    services.snapshots[services.source][RU + "toc.yaml"] = b"title: New\nitems: []\n"

    result = services.translate()
    assert result.verdict is Verdict.RED
    checkpoint = services.checkpoint()
    assert checkpoint.state.stage is ContinuationStage.REVIEW
    assert checkpoint.state.target_sha is None
    assert any(path.value.endswith("toc.yaml") for path in checkpoint.state.review_paths)


def test_toc_only_null_checkpoint_is_continuable() -> None:
    """REQUIREMENTS §5.3: TOC-only RED with empty Markdown scope continues (#3)."""

    class NullToc(ContinueServices):
        def model(self, request):
            body = json.loads(request.body)
            props = request_schema(body)["schema"]["properties"]
            role = (
                "direction"
                if "translation_required" in props
                else "critic"
                if "files" in props
                else "arbiter"
                if "findings" in props
                else "toc"
                if "strings" in props
                else "translate"
            )
            if role == "critic" and not self.continuing:
                self.roles.append(role)
                return HttpResponse(503, b"{}", None)
            response = super().model(request)
            if role == "toc":
                return HttpResponse(
                    200,
                    replace_response_text(response.body, json.dumps({"strings": {}})),
                    Decimal(".01"),
                )
            return response

    services = NullToc(names=(), stop="rename_red")
    services.changes = [{"status": "modified", "filename": RU + "toc.yaml"}]
    services.snapshots[services.base][RU + "toc.yaml"] = b"title: Old\nitems: []\n"
    services.snapshots[services.base][EN + "toc.yaml"] = b"title: Old EN\nitems: []\n"
    services.snapshots[services.source][RU + "toc.yaml"] = b"title: New\nitems: []\n"

    assert services.translate().verdict is Verdict.RED
    checkpoint = services.checkpoint()
    assert checkpoint.scope_target_paths == ()
    assert checkpoint.state.target_sha is None

    services.continuing = True
    services.stop = None
    services.roles.clear()
    resumed = services.resume()
    assert resumed.verdict in {Verdict.GREEN, Verdict.YELLOW, Verdict.RED}
    assert services.roles  # models ran; empty-Markdown continue admitted


def test_resource_only_red_checkpoint_is_continuable() -> None:
    """REQUIREMENTS §5.3: resource-only RED with empty Markdown scope continues (#4)."""

    class ResOnly(ContinueServices):
        def model(self, request):
            body = json.loads(request.body)
            props = request_schema(body)["schema"]["properties"]
            role = (
                "direction"
                if "translation_required" in props
                else "critic"
                if "files" in props
                else "arbiter"
                if "findings" in props
                else "toc"
                if "strings" in props
                else "translate"
            )
            if role == "critic" and not self.continuing:
                self.roles.append(role)
                payload = {
                    "model": "t",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"role": "assistant", "content": '{"files":{}}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
                return HttpResponse(200, json.dumps(payload).encode(), Decimal(".01"))
            return super().model(request)

    png = RU + "chart.png"
    services = ResOnly(names=(), stop="rename_red")
    blob = b"\x89PNG\x00\xff"
    services.changes = [{"status": "added", "filename": png}]
    for tree in [services.files, *services.snapshots.values()]:
        tree[png] = blob

    assert services.translate().verdict is Verdict.RED
    checkpoint = services.checkpoint()
    assert checkpoint.scope_target_paths == ()
    assert any(path.value.endswith("chart.png") for path in checkpoint.state.review_paths)

    services.continuing = True
    services.stop = None
    services.roles.clear()
    resumed = services.resume()
    assert resumed.verdict in {Verdict.GREEN, Verdict.YELLOW, Verdict.RED}
    assert services.roles


def test_delete_only_non_final_checkpoint_is_continuable() -> None:
    """REQUIREMENTS §4/§5.3: delete-only NON_FINAL opens a working continue (#3 tip)."""

    class DeleteOnly(ContinueServices):
        def model(self, request):
            body = json.loads(request.body)
            props = request_schema(body)["schema"]["properties"]
            role = (
                "direction"
                if "translation_required" in props
                else "critic"
                if "files" in props
                else "arbiter"
                if "findings" in props
                else "toc"
                if "strings" in props
                else "translate"
            )
            if role == "arbiter" and not self.continuing:
                self.roles.append(role)
                payload = {
                    "model": "t",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"role": "assistant", "content": '{"verdict":"GREEN","findings":[]}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
                return HttpResponse(200, json.dumps(payload).encode(), Decimal(".01"))
            return super().model(request)

    services = DeleteOnly(names=("page",), stop="rename_red")
    services.changes = [{"status": "removed", "filename": RU + "page.md"}]
    for tree in [services.files, *services.snapshots.values()]:
        tree.pop(RU + "page.md", None)

    assert services.translate().verdict is Verdict.RED
    checkpoint = services.checkpoint()
    assert any(path.value == "resource-review" for path in checkpoint.state.review_paths)

    services.continuing = True
    services.stop = None
    services.roles.clear()
    resumed = services.resume()
    assert resumed.verdict in {Verdict.GREEN, Verdict.YELLOW, Verdict.RED}


def test_mixed_markdown_and_binary_continue_skips_asset_utf8_restore() -> None:
    """REQUIREMENTS §5.3: continue restores binaries by bytes, not Markdown plan (#5)."""

    class Mix(ContinueServices):
        def model(self, request):
            body = json.loads(request.body)
            props = request_schema(body)["schema"]["properties"]
            role = (
                "direction"
                if "translation_required" in props
                else "critic"
                if "files" in props
                else "arbiter"
                if "findings" in props
                else "toc"
                if "strings" in props
                else "translate"
            )
            response = super().model(request)
            if role == "arbiter" and not self.continuing:
                payload = {
                    "verdict": "RED",
                    "findings": [
                        {
                            "reason": "Needs review.",
                            "expected_correction": "Fix translation.",
                            "searchable_snippet": "Translated",
                            "target_path": EN + "a.md",
                            "target_line": 1,
                        }
                    ],
                }
                return HttpResponse(
                    200,
                    replace_response_text(response.body, json.dumps(payload)),
                    Decimal(".01"),
                )
            return response

    png = RU + "chart.png"
    services = Mix(names=("a",), stop="rename_red")
    blob = b"\x89PNG\x00\xff"
    services.changes = [
        {"status": "modified", "filename": RU + "a.md"},
        {"status": "added", "filename": png},
    ]
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + "a.md"] = b"# Source\n"
        tree[png] = blob

    assert services.translate().verdict is Verdict.RED
    assert services.files[EN + "chart.png"] == blob
    services.continuing = True
    services.stop = None
    services.roles.clear()
    resumed = services.resume()
    assert resumed.verdict in {Verdict.GREEN, Verdict.YELLOW, Verdict.RED}
    assert services.files[EN + "chart.png"] == blob
    assert services.roles


def test_continue_still_zero_commit_red_keeps_null_checkpoint_and_reports() -> None:
    """REQUIREMENTS §4.2/§5.3/§7: continue from target_sha=null that stays zero-commit RED.

    Must not crash on review_checkpoint_head_mismatch, must reopen null checkpoint,
    and must publish a RED QA comment on the source PR.
    """

    class AlwaysFailTranslate(ContinueServices):
        def model(self, request):
            body = json.loads(request.body)
            props = request_schema(body)["schema"]["properties"]
            role = (
                "direction"
                if "translation_required" in props
                else "critic"
                if "files" in props
                else "arbiter"
                if "findings" in props
                else "toc"
                if "strings" in props
                else "translate"
            )
            if role == "direction":
                return super().model(request)
            if role == "translate":
                return HttpResponse(503, b"{}", None)
            if role == "critic":
                payload = {
                    "model": "t",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"role": "assistant", "content": '{"files":{}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
                return HttpResponse(200, json.dumps(payload).encode(), Decimal(".01"))
            return super().model(request)

    services = AlwaysFailTranslate(names=("page",), stop="rename_red")
    services.snapshots[services.source][RU + "page.md"] = b"# Source page\n\nBody text.\n"
    services.snapshots[services.base][RU + "page.md"] = b"# Old\n"

    assert services.translate().verdict is Verdict.RED
    assert services.commits == 0
    first = services.checkpoint()
    assert first.state.target_sha is None
    assert first.state.stage is ContinuationStage.REVIEW

    services.continuing = True
    services.stop = None
    services.roles.clear()
    resumed = services.resume()
    assert resumed.verdict is Verdict.RED
    assert services.commits == 0
    following = services.checkpoint()
    assert following.state.target_sha is None
    assert following.state.stage is ContinuationStage.REVIEW
    assert any(
        "<!-- ydbdoc-current-qa -->" in comment["body"] and comment["body"].startswith("🔴 RED")
        for comment in services.source_comments
    )
