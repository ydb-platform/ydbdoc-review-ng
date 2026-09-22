"""Review continuation through the public runtime, with remote I/O fakes only."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from test_continue_translation import CONTEXT, EN, RU, LifecycleServices

from ydbdoc_review_ng import application
from ydbdoc_review_ng.continuation import (
    ContinuationStage,
    candidate_sha256,
    decode_state,
    encode_state,
)
from ydbdoc_review_ng.domain import GitSha
from ydbdoc_review_ng.models import HttpResponse
from ydbdoc_review_ng.persistence import PersistenceError
from ydbdoc_review_ng.quality import Verdict
from ydbdoc_review_ng.runtime_content import pack


class ReviewServices(LifecycleServices):
    def __init__(self, **kwargs):
        super().__init__(stop="review", **kwargs)
        self.outcomes = {}
        self.calls = []
        self.timeline = []
        self.repair_payload = None
        self.move_after = None
        self.waiting_ci = False
        self.move_during_report = False
        for tree in [self.files, *self.snapshots.values()]:
            tree[RU + "a.md"] += b"\n```sql\nSELECT 1;\n```\n"
            tree[RU + "b.md"] += b"\nSource detail b\n"

    def start_review(self):
        assert self.translate().verdict is Verdict.RED
        saved = self.checkpoint()
        self.original = saved.continuation_id
        self.continuing = True
        self.stop = None
        self.roles.clear()
        self.prompts.clear()
        self.events.clear()
        self.operations.clear()
        self.parents.clear()
        self.timeline.clear()
        self.reads.clear()
        self.critics = 0
        self.initial_commits = self.commits
        return saved

    def execute(self, statement, parameters):
        if self.continuing and "/continuations`" in statement and "UPSERT" in statement:
            self.timeline.append("checkpoint")
        return super().execute(statement, parameters)

    def github(self, method, path, payload):
        if self.continuing:
            if method == "POST" and path.endswith("/git/commits"):
                self.timeline.append("commit")
            if method == "PATCH" and "/git/refs/" in path:
                self.timeline.append("push")
            if method in {"POST", "PATCH"} and "/comments" in path:
                self.timeline.append("report")
                if self.failure == "report":
                    raise OSError("report unavailable")
            if self.waiting_ci and "/check-runs?" in path:
                return {"total_count": 0, "check_runs": []}
            if self.move_during_report and path.endswith("/issues/43/comments?per_page=100"):
                self.branch_head = "f" * 40
        return super().github(method, path, payload)

    def model(self, request):
        if not self.continuing:
            return super().model(request)
        body = json.loads(request.body)
        schema = body["jsonSchema"]["schema"]
        prompt = body["messages"][-1]["text"]
        path = prompt.split("Target path: ", 1)[1].split("\n", 1)[0]
        role = "critic" if "verdict" in schema["properties"] else "repair"
        self.roles.append(role)
        self.prompts.append((role, prompt))
        self.calls.append((role, path, schema))
        self.timeline.append(role)
        if self.failure == role:
            raise TimeoutError("model unavailable")
        if role == "critic":
            outcomes = self.outcomes.get(path, [])
            outcome = outcomes.pop(0) if outcomes else "green"
            values = {"verdict": "GREEN" if outcome == "green" else "RED", "findings": []}
            if outcome != "green":
                target = self.snapshots[self.branch_head][path].decode()
                finding = {
                    "repairable": outcome == "repair",
                    "reason": "Missing meaning in the heading.",
                    "expected_correction": "Restore the full meaning.",
                    "searchable_snippet": target.splitlines()[0].removeprefix("# "),
                    "target_path": path,
                    "target_line": 1,
                }
                if outcome == "repair":
                    ids = schema["properties"]["findings"]["items"]["properties"]["field_ids"]
                    finding["field_ids"] = ids["items"]["enum"][:1]
                values["findings"].append(finding)
        else:
            fields, _ = json.JSONDecoder().raw_decode(prompt.split("Allowed fields: ", 1)[1])
            values = {
                item["field_id"]: item["source_field_text"].replace("Source", "Repaired")
                for item in fields
            }
        if self.move_after == role:
            self.branch_head = "f" * 40
            self.snapshots[self.branch_head] = dict(self.files)
        raw = (
            self.repair_payload if role == "repair" and self.repair_payload else json.dumps(values)
        )
        return HttpResponse(
            200,
            json.dumps(
                {
                    "result": {
                        "alternatives": [
                            {
                                "status": "ALTERNATIVE_STATUS_FINAL",
                                "message": {
                                    "role": "assistant",
                                    "text": raw,
                                },
                            }
                        ],
                        "usage": {"inputTextTokens": "10", "completionTokens": "5"},
                    }
                }
            ).encode(),
            Decimal("0.01"),
        )


def test_green_review_only_updates_current_verdict_and_consumes_without_commit(capsys):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    before = dict(services.files)
    saved_state = services.rows[saved.continuation_id]["state"]
    result = services.resume(43)
    assert result.verdict is Verdict.GREEN and result.final_commit_sha == saved.target_sha
    assert services.roles == ["critic"]
    assert [path for _, path, _ in services.calls] == [EN + "b.md"]
    assert services.files == before
    assert services.files[EN + "a.md"] == b"# Corrected\n\n```sql\nSELECT 1;\n```\n"
    assert services.rows[saved.continuation_id]["state"] == saved_state
    assert services.commits == services.initial_commits
    assert services.timeline == ["critic", "report"]
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.rows[saved.continuation_id]["consumed_by_job_id"] == result.job_id
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert len(services.comments) == 1 and services.comments[0]["body"].startswith("GREEN\n")
    assert saved.target_sha.value in services.comments[0]["body"]
    assert all(CONTEXT in prompt for _, prompt in services.prompts)
    prompt = services.prompts[0][1]
    source = prompt.split("<authoritative-source>\n")[1].split("</authoritative-source>")[0]
    assert source == "# Source b\n\nSource detail b\n"
    assert CONTEXT not in services.comments[0]["body"]
    assert CONTEXT not in capsys.readouterr().out
    attempts = [params for _, params in services.operations if "attempt_id" in params]
    assert len(attempts) == 1 and attempts[0]["job_id"] == result.job_id
    assert attempts[0]["cost_rub"] == Decimal("0.01")
    assert not any("SUM" in query for query, _ in services.operations)
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == ["critic"]


def test_repair_replaces_full_document_map_and_publishes_before_final_critic():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    green = services.files[EN + "a.md"]
    services.outcomes = {EN + "b.md": ["repair", "red"]}
    services.rows[saved.continuation_id]["created_at"] -= timedelta(days=5)
    saved = services.checkpoint()
    result = services.resume()
    following = services.checkpoint()
    assert result.verdict is Verdict.RED and result.repair_applied
    assert services.roles == ["critic", "repair", "critic"]
    assert services.timeline == [
        "critic",
        "repair",
        "commit",
        "push",
        "critic",
        "report",
        "checkpoint",
    ]
    assert services.files[EN + "a.md"] == green
    assert services.files[EN + "b.md"] == b"# Repaired b\n\nRepaired detail b\n"
    assert following.state.accepted_maps[0] == saved.state.accepted_maps[0]
    assert set(following.state.accepted_maps[1].as_dict().values()) == {
        "Repaired b",
        "Repaired detail b",
    }
    assert len(services.calls[1][2]["properties"]) == 2
    assert following.target_sha == result.final_commit_sha
    assert following.state.candidate_sha256 == candidate_sha256(
        pack(
            {
                EN + "a.md": green,
                EN + "b.md": b"# Repaired b\n\nRepaired detail b\n",
            }
        )
    )
    assert following.created_at == saved.created_at and following.expires_at == saved.expires_at
    assert following.job_id == result.job_id
    assert following.state.stage is ContinuationStage.REVIEW
    assert following.state.review_paths == saved.state.review_paths
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.parents == [[saved.target_sha.value]]
    assert all(CONTEXT in prompt for _, prompt in services.prompts)
    assert all(CONTEXT.encode() not in value for value in services.files.values())
    assert len(services.comments) == 1 and services.comments[0]["body"].startswith("RED\n")
    assert result.final_commit_sha.value in services.comments[0]["body"]
    second = services.resume()
    assert second.verdict is Verdict.GREEN and second.final_commit_sha == result.final_commit_sha
    assert services.commits == services.initial_commits + 1
    assert services.roles == ["critic", "repair", "critic", "critic"]
    assert services.rows[following.continuation_id]["status"] == "closed"


def test_saved_order_and_single_repair_across_unresolved_documents():
    services = ReviewServices()
    saved = services.start_review()
    row = services.rows[saved.continuation_id]
    state = json.loads(row["state"])
    state["review_paths"] = [EN + "c.md", EN + "b.md"]
    row["state"] = encode_state(decode_state(json.dumps(state))).encode()
    services.outcomes = {EN + "c.md": ["repair", "green"], EN + "b.md": ["repair"]}
    result = services.resume()
    assert result.verdict is Verdict.RED
    assert [(role, path) for role, path, _ in services.calls] == [
        ("critic", EN + "c.md"),
        ("repair", EN + "c.md"),
        ("critic", EN + "c.md"),
        ("critic", EN + "b.md"),
    ]
    following = services.checkpoint()
    assert [p.value for p in following.state.review_paths] == [EN + "b.md"]
    assert following.state.accepted_maps[:2] == saved.state.accepted_maps[:2]
    assert services.files[EN + "b.md"] == b"# Translated\n\nTranslated\n"


def test_repeated_red_preserves_unresolved_path_order_for_the_next_continue():
    services = ReviewServices()
    saved = services.start_review()
    row = services.rows[saved.continuation_id]
    state = json.loads(row["state"])
    state["review_paths"] = [EN + "c.md", EN + "b.md"]
    row["state"] = encode_state(decode_state(json.dumps(state))).encode()
    services.outcomes = {EN + "c.md": ["red"], EN + "b.md": ["red"]}
    result = services.resume()
    assert result.verdict is Verdict.RED
    following = services.checkpoint()
    assert [p.value for p in following.state.review_paths] == [EN + "c.md", EN + "b.md"]
    assert following.state.accepted_maps == saved.state.accepted_maps
    assert following.target_sha == saved.target_sha
    assert following.state.candidate_sha256 == saved.state.candidate_sha256
    assert services.resume().verdict is Verdict.GREEN
    assert [path for _, path, _ in services.calls] == [
        EN + "c.md",
        EN + "b.md",
        EN + "c.md",
        EN + "b.md",
    ]


@pytest.mark.parametrize("repair", [False, True])
def test_pinned_rename_review_never_derives_maps_from_target_and_replays_metadata(
    repair, monkeypatch
):
    services = ReviewServices(names=("a", "b"))
    services.stop = "rename_red"
    services.changes[0] = {
        "status": "renamed",
        "filename": RU + "a.md",
        "previous_filename": RU + "old.md",
        "changes": 0,
    }
    pinned = b"# Whole pinned translation\n\n```sql\nSELECT 1;\n```\n"
    for tree in [services.files, *services.snapshots.values()]:
        tree.pop(EN + "a.md")
        tree[EN + "old.md"] = pinned
        tree[EN + "toc.yaml"] = b"items:\n  - name: Old\n    href: old.md\n"
    saved = services.start_review()
    assert [item.target_path.value for item in saved.state.accepted_maps] == [EN + "b.md"]
    metadata = {path: value for path, value in services.files.items() if path.endswith(".yaml")}

    def forbidden(*args, **kwargs):
        raise AssertionError("REVIEW cannot reconstruct a map from target bytes")

    monkeypatch.setattr("ydbdoc_review_ng.quality.repair._derive_target_translations", forbidden)
    services.outcomes = {EN + "a.md": ["repair", "red"] if repair else ["red"]}
    result = services.resume()
    assert result.verdict is Verdict.RED
    assert services.roles == (
        ["critic", "repair", "critic", "critic"] if repair else ["critic", "critic"]
    )
    following = services.checkpoint()
    assert following.state.review_paths == saved.state.review_paths[:1]
    assert services.files[EN + "a.md"] == (
        b"# Repaired a\n\n```sql\nSELECT 1;\n```\n" if repair else pinned
    )
    assert {
        path: value for path, value in services.files.items() if path.endswith(".yaml")
    } == metadata
    assert EN + "old.md" not in services.files
    assert len(following.state.accepted_maps) == (2 if repair else 1)
    assert services.resume().verdict is Verdict.GREEN
    assert services.commits == services.initial_commits + int(repair)


@pytest.mark.parametrize("fault", ["head", "hash", "map", "candidate", "path"])
def test_admission_rejects_stale_or_unreconstructible_candidate_before_models(fault):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    row = services.rows[saved.continuation_id]
    state = json.loads(row["state"])
    if fault == "head":
        services.branch_head = "f" * 40
    elif fault == "hash":
        state["candidate_sha256"] = "0" * 64
    elif fault == "map":
        state["accepted_maps"][EN + "a.md"] = {"unknown": "Wrong"}
    elif fault == "candidate":
        services.snapshots[services.branch_head][EN + "a.md"] = b"# Tampered\n"
    else:
        state["review_paths"] = [EN + "outside.md"]
    row["state"] = json.dumps(state).encode()
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == []
    assert not any(method in {"POST", "PATCH"} for method, _ in services.events)
    assert set(services.rows) == {saved.continuation_id}
    assert services.rows[saved.continuation_id]["status"] == "open"


@pytest.mark.parametrize("failure", ["critic", "repair", "publish", "report", "attempt"])
def test_infrastructure_failure_preserves_old_checkpoint(failure):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.outcomes = {EN + "b.md": ["repair", "red"]}
    services.failure = failure
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert set(services.rows) == {saved.continuation_id}
    assert services.rows[saved.continuation_id]["status"] == "open"
    assert services.jobs[list(services.jobs)[-1]]["status"] == "failed"
    assert "checkpoint" not in services.timeline
    assert (
        services.roles
        == {
            "critic": ["critic"],
            "repair": ["critic", "repair"],
            "publish": ["critic", "repair"],
            "report": ["critic", "repair", "critic"],
            "attempt": ["critic"],
        }[failure]
    )


@pytest.mark.parametrize("move_after", ["critic", "repair"])
def test_head_movement_blocks_later_models_publication_and_verdict(move_after):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.move_after = move_after
    services.outcomes = {EN + "b.md": ["repair"]}
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == (["critic"] if move_after == "critic" else ["critic", "repair"])
    assert "report" not in services.timeline and "commit" not in services.timeline
    assert services.rows[saved.continuation_id]["status"] == "open"


def test_invalid_full_map_keeps_candidate_and_still_gets_final_critic():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    before = dict(services.files)
    services.outcomes = {EN + "b.md": ["repair", "red"]}
    first_id = saved.state.accepted_maps[1].fields[0][0]
    services.repair_payload = json.dumps({first_id: "Only one field"})
    result = services.resume()
    assert result.verdict is Verdict.RED and not result.repair_applied
    assert services.roles == ["critic", "repair", "critic"]
    assert services.files == before and services.commits == services.initial_commits
    assert services.checkpoint().state.accepted_maps == saved.state.accepted_maps


def test_byte_identical_full_repair_reports_existing_sha_without_empty_commit():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.outcomes = {EN + "b.md": ["repair", "green"]}
    services.repair_payload = json.dumps(saved.state.accepted_maps[1].as_dict())
    result = services.resume()
    assert result.verdict is Verdict.GREEN and result.repair_applied
    assert result.final_commit_sha == saved.target_sha
    assert services.roles == ["critic", "repair", "critic"]
    assert services.commits == services.initial_commits
    assert services.timeline == ["critic", "repair", "critic", "report"]
    assert services.rows[saved.continuation_id]["status"] == "closed"


def test_head_movement_while_loading_comment_blocks_stale_verdict_and_close():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.move_during_report = True
    with pytest.raises(application.WorkflowError, match="report"):
        services.resume()
    assert services.roles == ["critic"]
    assert "report" not in services.timeline
    assert services.rows[saved.continuation_id]["status"] == "open"
    assert set(services.rows) == {saved.continuation_id}


@pytest.mark.parametrize("create", [False, True], ids=["patch", "post"])
@pytest.mark.parametrize("verdict", ["green", "red"])
@pytest.mark.parametrize("new_head", ["f" * 40, None], ids=["moved", "deleted"])
def test_head_change_during_comment_write_fails_without_consuming_checkpoint(
    create, verdict, new_head
):
    class ChangedDuringComment(ReviewServices):
        change_head = True

        def github(self, method, path, payload):
            response = super().github(method, path, payload)
            if (
                self.continuing
                and self.change_head
                and method in {"POST", "PATCH"}
                and "/comments" in path
            ):
                self.branch_head = new_head
            return response

    services = ChangedDuringComment(names=("a", "b"))
    saved = services.start_review()
    if create:
        services.comments.clear()
    services.outcomes = {EN + "b.md": [verdict]}
    with pytest.raises(application.WorkflowError, match="report"):
        services.resume()
    failed = list(services.jobs.values())[-1]
    assert failed["status"] == "failed" and failed["error"] == "report_failed"
    assert services.roles == ["critic"] and services.timeline == ["critic", "report"]
    assert services.branch_head == new_head
    assert set(services.rows) == {saved.continuation_id}
    assert services.rows[saved.continuation_id]["status"] == "open"
    assert services.rows[saved.continuation_id].get("consumed_by_job_id") is None
    assert not any(
        "SET consumed_by_job_id" in query or "SET status = 'closed'" in query
        for query, _ in services.operations
    )
    # The completed remote write remains visible but is tied to the failed run's SHA.
    assert len(services.comments) == 1
    comment_id = services.comments[0]["id"]
    assert services.comments[0]["body"].startswith(verdict.upper() + "\n")
    assert saved.target_sha.value in services.comments[0]["body"]
    # Restoring the exact saved head makes a later run valid. It updates that same comment.
    services.change_head = False
    services.branch_head = saved.target_sha.value
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert len(services.comments) == 1 and services.comments[0]["id"] == comment_id
    assert services.comments[0]["body"].startswith("GREEN\n")
    assert services.roles == ["critic", "critic"]


@pytest.mark.parametrize("mode", ["continue", "verify"])
@pytest.mark.parametrize("new_head", [None, "f" * 40], ids=["deleted", "moved"])
def test_pinned_branch_change_during_repair_commit_cannot_create_or_update_ref(mode, new_head):
    class ChangedDuringCommit(ReviewServices):
        def github(self, method, path, payload):
            response = super().github(method, path, payload)
            if self.continuing and method == "POST" and path.endswith("/git/commits"):
                self.branch_head = new_head
            return response

    services = ChangedDuringCommit(names=("a", "b"))
    saved = services.start_review()
    services.outcomes = {EN + "b.md": ["repair", "green"]}
    with pytest.raises(application.WorkflowError, match="publish"):
        if mode == "continue":
            services.resume()
        else:
            services.runtime().doc_verify(
                application.VerifyWorkflowInput(
                    43,
                    GitSha(services.source),
                    saved.target_sha,
                )
            )
    assert services.roles == (
        ["critic", "repair"] if mode == "continue" else ["critic", "critic", "repair"]
    )
    assert services.timeline[-1] == "commit"
    assert services.commits == services.initial_commits + 1
    assert services.branch_head == new_head
    assert not any(
        method in {"POST", "PATCH"} and "/git/refs" in path for method, path in services.events
    )
    assert set(services.rows) == {saved.continuation_id}
    assert services.rows[saved.continuation_id]["status"] == "open"
    assert services.rows[saved.continuation_id].get("consumed_by_job_id") is None
    assert list(services.jobs.values())[-1]["status"] == "failed"


def test_initial_translate_without_target_still_creates_branch():
    services = ReviewServices(names=("a", "b"))
    services.stop = None
    assert services.branch_head is None
    result = services.translate()
    assert result.verdict is Verdict.GREEN
    assert services.branch_head == result.final_commit_sha.value
    assert services.roles == ["translate", "translate", "translate", "critic", "critic"]
    assert (
        sum(method == "POST" and path.endswith("/git/refs") for method, path in services.events)
        == 1
    )


def test_green_review_waiting_for_ci_reports_yellow_and_closes_semantic_checkpoint():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.waiting_ci = True
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.comments[0]["body"].startswith("YELLOW\n")
    assert services.rows[saved.continuation_id]["status"] == "closed"


@pytest.mark.parametrize("fault", ["close_before", "close_after", "success_after"])
def test_review_green_consumption_survives_lost_lifecycle_acknowledgements(fault):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.fault = fault
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert services.rows[saved.continuation_id]["consumed_by_job_id"] == result.job_id
    with pytest.raises(PersistenceError):
        services.checkpoint()
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == ["critic"]


@pytest.mark.parametrize("fault", ["activate_before", "activate_after", "close_before"])
def test_review_red_handoff_preserves_one_logical_checkpoint_after_boundary_fault(fault):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.outcomes = {EN + "b.md": ["red"]}
    services.fault = fault
    if fault == "activate_before":
        with pytest.raises(application.WorkflowError):
            services.resume()
    else:
        assert services.resume().verdict is Verdict.RED
    consuming = list(services.jobs)[-1]
    services.fault = None
    eligible = services.checkpoint()
    assert eligible.continuation_id == (
        saved.continuation_id if fault == "activate_before" else consuming
    )
    assert eligible.target_sha == saved.target_sha
    assert eligible.expires_at == saved.expires_at
    assert eligible.state.accepted_maps == saved.state.accepted_maps
    assert services.roles == ["critic"]
    assert services.resume().verdict is Verdict.GREEN
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == ["critic", "critic"]
