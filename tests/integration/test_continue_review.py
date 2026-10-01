"""Review continuation through the public runtime, with remote I/O fakes only."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from _runtime_services import (
    raw_repair_context,
    replace_response_text,
    request_prompt,
    request_schema,
    response_text,
)
from test_continue_translation import CONTEXT, EN, RU, LifecycleServices

from ydbdoc_review_ng import application
from ydbdoc_review_ng.continuation import decode_state, encode_state
from ydbdoc_review_ng.domain import GitSha
from ydbdoc_review_ng.models import HttpResponse
from ydbdoc_review_ng.persistence import PersistenceError
from ydbdoc_review_ng.quality import Verdict


class ReviewServices(LifecycleServices):
    def __init__(self, **kwargs):
        super().__init__(stop="review", **kwargs)
        self.outcomes = {}
        self.calls = []
        self.timeline = []
        self.repair_payload = None
        self.repair_uses_current_values = False
        self.move_after = None
        self.waiting_ci = False
        self.move_during_report = False
        self.arbiter_outcomes = {}
        for tree in [self.files, *self.snapshots.values()]:
            tree[RU + "a.md"] += b"\n```sql\nSELECT 1;\n```\n"
            tree[RU + "b.md"] += b"\nSource detail b\n"
            tree[RU + "concepts/glossary.md"] = ("# Glossary RU\n" + "Definition\n" * 900).encode()
            tree[EN + "concepts/glossary.md"] = b"# Glossary EN\nFull definition\n"

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
        schema = request_schema(body)["schema"]
        prompt = request_prompt(body)
        role = "critic" if "files" in schema["properties"] else "arbiter"
        files = json.loads(raw_repair_context(prompt, "translation-pr-files"))
        sources = json.loads(raw_repair_context(prompt, "source-pr-files"))
        self.roles.append(role)
        self.prompts.append((role, prompt))
        self.calls.append((role, tuple(files), schema))
        self.timeline.append(role)
        if self.failure == role:
            raise TimeoutError("model unavailable")
        findings = []
        for path, current in files.items():
            if role == "critic":
                outcomes = self.outcomes.get(path, [])
                outcome = outcomes.pop(0) if outcomes else "green"
                self.arbiter_outcomes[path] = outcome
                if outcome == "repair" and not self.repair_uses_current_values:
                    source = sources[path.replace("/en/", "/ru/")]
                    if current.startswith("# Whole pinned translation"):
                        files[path] = source.replace("Source", "Repaired")
                    else:
                        files[path] = source.splitlines(keepends=True)[0].replace(
                            "Source", "Repaired"
                        ) + "".join(current.splitlines(keepends=True)[1:])
                if outcome == "repair" and self.failure == "repair":
                    raise TimeoutError("model unavailable")
                if outcome == "repair" and self.move_after == "repair":
                    self.branch_head = "f" * 40
                    self.snapshots[self.branch_head] = dict(self.files)
            elif self.arbiter_outcomes.get(path) in {"red", "yellow"}:
                findings.append(
                    {
                        "reason": "Missing meaning in the heading.",
                        "expected_correction": "Restore the full meaning.",
                        "searchable_snippet": current.splitlines()[0],
                        "target_path": path,
                        "target_line": 1,
                    }
                )
        values = (
            {"files": files}
            if role == "critic"
            else {
                "verdict": (
                    "RED"
                    if any(self.arbiter_outcomes.get(p) == "red" for p in files)
                    else "YELLOW"
                    if findings
                    else "GREEN"
                ),
                "findings": findings,
            }
        )
        if self.move_after == role:
            self.branch_head = "f" * 40
            self.snapshots[self.branch_head] = dict(self.files)
        raw = (
            self.repair_payload
            if role == "critic" and self.repair_payload is not None
            else json.dumps(values)
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


def test_continue_yellow_closes_checkpoint_without_repair():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.outcomes = {EN + "b.md": ["yellow"]}
    result = services.resume()
    assert result.verdict is Verdict.YELLOW
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.roles == ["critic", "arbiter"]
    assert services.commits == services.initial_commits
    assert "Missing meaning in the heading." in services.comments[0]["body"]


def metadata_review_services(toc_name="toc.yaml", href="a.md"):
    services = ReviewServices(names=("a", "b"))
    services.changes.append({"status": "modified", "filename": RU + toc_name})
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + toc_name] = f"items:\n  - name: Source title\n    href: {href}\n".encode()
        tree[EN + toc_name] = b"items: []\n"
    services.changes[0]["status"] = "added"
    services.snapshots[services.base][RU + toc_name] = b"items:\n"
    return services


@pytest.mark.parametrize("toc_name,href", [("toc.yaml", "a.md"), ("nav/toc.yaml", "../a.md")])
def test_continue_preserves_complete_corrected_toc_with_residual_finding(toc_name, href):
    services = metadata_review_services(toc_name, href)
    saved = services.start_review()
    corrected = f"items:\n  - name: Corrected complete title\n    href: {href}\n"
    services.repair_payload = json.dumps(
        {
            "files": {
                EN + "a.md": services.files[EN + "a.md"].decode(),
                EN + "b.md": services.files[EN + "b.md"].decode(),
                EN + toc_name: corrected,
            }
        }
    )
    services.outcomes = {EN + toc_name: ["red"]}
    assert services.resume().verdict is Verdict.RED
    following = services.checkpoint()
    assert following.expires_at == saved.expires_at
    assert {
        item.target_path.value: item.translated_markdown
        for item in following.state.accepted_documents
    }[EN + toc_name] == corrected
    services.repair_payload = None
    services.prompts.clear()
    assert services.resume().verdict is Verdict.GREEN
    for _, prompt in services.prompts:
        assert (
            json.loads(raw_repair_context(prompt, "translation-pr-files"))[EN + toc_name]
            == corrected
        )
    assert services.files[EN + toc_name].decode() == corrected


@pytest.mark.parametrize("fault", ["navigation", "invalid_yaml", "outside", "binary"])
def test_review_metadata_replay_rejects_tampering_before_models(fault):
    from ydbdoc_review_ng.continuation import candidate_sha256
    from ydbdoc_review_ng.runtime_content import pack

    services = metadata_review_services()
    saved = services.start_review()
    row = services.rows[saved.continuation_id]
    state = json.loads(row["state"])
    documents = state["accepted_documents"]
    if fault == "navigation":
        documents[EN + "toc.yaml"] = documents[EN + "toc.yaml"].replace("a.md", "outside.md")
    elif fault == "invalid_yaml":
        documents[EN + "toc.yaml"] = "items: ["
    else:
        path = EN + ("toc_other.yaml" if fault == "outside" else "asset.png")
        documents[path] = documents.pop(EN + "toc.yaml")
    candidate = {path: text.encode() for path, text in documents.items()}
    state["candidate_sha256"] = candidate_sha256(pack(candidate)).value
    services.snapshots[saved.target_sha.value].update(candidate)
    row["state"] = json.dumps(state).encode()
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == []
    assert not any(method in {"POST", "PATCH"} for method, _ in services.events)
    assert services.rows[saved.continuation_id]["status"] == "open"


def test_green_review_only_updates_current_verdict_and_consumes_without_commit(capsys):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    before = dict(services.files)
    saved_state = services.rows[saved.continuation_id]["state"]
    result = services.resume(43)
    assert result.verdict is Verdict.GREEN and result.final_commit_sha == saved.target_sha
    assert services.roles == ["critic", "arbiter"]
    assert [paths for _, paths, _ in services.calls] == [(EN + "a.md", EN + "b.md")] * 2
    assert services.files == before
    assert services.files[EN + "a.md"] == b"# Corrected\n\n```sql\nSELECT 1;\n```\n"
    assert services.rows[saved.continuation_id]["state"] == saved_state
    assert services.commits == services.initial_commits
    assert services.timeline == ["critic", "arbiter", "report"]
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.rows[saved.continuation_id]["consumed_by_job_id"] == result.job_id
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert len(services.comments) == 1 and services.comments[0]["body"].startswith("🟢 GREEN\n")
    assert all(CONTEXT in prompt for _, prompt in services.prompts)
    prompt = services.prompts[0][1]
    source = json.loads(raw_repair_context(prompt, "source-pr-files"))
    assert source[RU + "b.md"] == "# Source b\n\nSource detail b\n"
    assert source[RU + "a.md"] == "# Source a\n\n```sql\nSELECT 1;\n```\n"
    for _, prompt in services.prompts:
        assert "Prior arbiter sentinel" not in prompt
        for tag in ("source-pr-files", "translation-pr-files", "project-glossary"):
            assert CONTEXT not in raw_repair_context(prompt, tag)
    assert CONTEXT not in services.comments[0]["body"]
    assert CONTEXT not in capsys.readouterr().out
    attempts = [params for _, params in services.operations if "attempt_id" in params]
    assert len(attempts) == 2 and all(item["job_id"] == result.job_id for item in attempts)
    assert attempts[0]["cost_rub"] == Decimal("0.01")
    assert not any("SUM" in query for query, _ in services.operations)
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == ["critic", "arbiter"]


def test_critic_editor_merges_complete_document_and_finishes_green():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    green = services.files[EN + "a.md"]
    services.outcomes = {EN + "b.md": ["repair"]}
    services.rows[saved.continuation_id]["created_at"] -= timedelta(days=5)
    saved = services.checkpoint()
    result = services.resume()
    assert result.verdict is Verdict.GREEN and result.repair_applied
    assert services.roles == ["critic", "arbiter"]
    assert services.timeline == [
        "critic",
        "arbiter",
        "commit",
        "push",
        "report",
    ]
    assert services.files[EN + "a.md"] == green
    assert services.files[EN + "b.md"] == b"# Repaired b\n\nTranslated\n"
    repair_prompt = services.prompts[0][1]
    assert services.calls[0][2] is not None
    assert "Source b" in raw_repair_context(repair_prompt, "source-pr-files")
    assert "Translated" in raw_repair_context(repair_prompt, "translation-pr-files")
    assert "Source detail b" in repair_prompt
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert services.parents == [[saved.target_sha.value]]
    assert all(CONTEXT in prompt for _, prompt in services.prompts)
    assert all(CONTEXT.encode() not in value for value in services.files.values())
    assert len(services.comments) == 1 and services.comments[0]["body"].startswith("🟢 GREEN\n")
    assert services.commits == services.initial_commits + 1
    with pytest.raises(application.WorkflowError):
        services.resume()


def test_continuation_repair_publishes_exact_model_markdown():
    class FormattingReviewServices(ReviewServices):
        def model(self, request):
            response = super().model(request)
            role = self.roles[-1]
            body = json.loads(request.body)
            prompt = request_prompt(body)
            replacement = None
            if role == "translate" and "Nested source item" in prompt:
                replacement = "* Parent translated\n* Nested translated item\n"
            elif role == "critic" and self.continuing and "Nested source item" in prompt:
                values = json.loads(response_text(response.body))
                values["files"][EN + "b.md"] = "* Parent corrected\n* Nested translated item\n"
                return HttpResponse(
                    200, replace_response_text(response.body, json.dumps(values)), Decimal("0.01")
                )
            if replacement is None:
                return response
            return HttpResponse(
                200, replace_response_text(response.body, replacement), Decimal("0.01")
            )

    services = FormattingReviewServices(names=("a", "b"))
    source = b"* Parent\n  * Nested source item\n"
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + "b.md"] = source

    services.start_review()
    services.outcomes = {EN + "b.md": ["repair"]}

    result = services.resume()

    expected = b"* Parent corrected\n* Nested translated item\n"
    assert result.verdict is Verdict.GREEN and result.repair_applied
    assert services.roles == ["critic", "arbiter"]
    assert services.files[EN + "b.md"] == expected
    assert services.snapshots[result.final_commit_sha.value][EN + "b.md"] == expected


def test_saved_review_paths_do_not_narrow_the_complete_review():
    services = ReviewServices()
    saved = services.start_review()
    row = services.rows[saved.continuation_id]
    state = json.loads(row["state"])
    state["review_paths"] = [EN + "c.md", EN + "b.md"]
    row["state"] = encode_state(decode_state(json.dumps(state))).encode()
    services.outcomes = {EN + "c.md": ["repair"], EN + "b.md": ["repair"]}
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert [(role, path) for role, path, _ in services.calls] == [
        ("critic", (EN + "a.md", EN + "b.md", EN + "c.md")),
        ("arbiter", (EN + "a.md", EN + "b.md", EN + "c.md")),
    ]
    assert services.files[EN + "c.md"] == b"# Repaired c\n"
    assert services.files[EN + "b.md"] == b"# Repaired b\n\nTranslated\n"
    assert services.commits == services.initial_commits + 1
    with pytest.raises(PersistenceError):
        services.checkpoint()


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
    assert following.state.accepted_documents == saved.state.accepted_documents
    assert following.target_sha == saved.target_sha
    assert following.state.candidate_sha256 == saved.state.candidate_sha256
    assert services.resume().verdict is Verdict.GREEN
    assert [paths for _, paths, _ in services.calls] == [
        (EN + "a.md", EN + "b.md", EN + "c.md")
    ] * 4


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
    source = b"# Source a\n\nSource detail a\n\n```sql\nSELECT 1;\n```\n"
    pinned = b"# Whole pinned translation\n\nWhole pinned detail\n\n```sql\nSELECT 1;\n```\n"
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + "a.md"] = source
        tree.pop(EN + "a.md")
        tree[EN + "old.md"] = pinned
        tree[EN + "toc.yaml"] = b"items:\n  - name: Old\n    href: old.md\n"
    saved = services.start_review()
    assert [item.target_path.value for item in saved.state.accepted_documents] == [
        EN + "a.md",
        EN + "b.md",
        EN + "toc.yaml",
    ]
    metadata = {path: value for path, value in services.files.items() if path.endswith(".yaml")}

    from ydbdoc_review_ng.quality.repair import _derive_target_translations

    def source_only_bridge(*args, **kwargs):
        assert args[3] != pinned, "old target bytes are context only"
        return _derive_target_translations(*args, **kwargs)

    monkeypatch.setattr(
        "ydbdoc_review_ng.quality.repair._derive_target_translations", source_only_bridge
    )
    services.outcomes = {EN + "a.md": ["repair", "red"] if repair else ["red"]}
    result = services.resume()
    assert result.verdict is (Verdict.GREEN if repair else Verdict.RED)
    assert services.roles == ["critic", "arbiter"]
    if repair:
        repair_prompt = services.prompts[0][1]
        assert services.calls[0][2] is not None
        assert "Source detail a" in raw_repair_context(repair_prompt, "source-pr-files")
        assert "Whole pinned detail" in raw_repair_context(repair_prompt, "translation-pr-files")
    assert services.files[EN + "a.md"] == (
        b"# Repaired a\n\nRepaired detail a\n\n```sql\nSELECT 1;\n```\n" if repair else pinned
    )
    assert {
        path: value for path, value in services.files.items() if path.endswith(".yaml")
    } == metadata
    assert EN + "old.md" not in services.files
    assert services.commits == services.initial_commits + int(repair)
    if repair:
        with pytest.raises(PersistenceError):
            services.checkpoint()
    else:
        following = services.checkpoint()
        assert following.state.review_paths == saved.state.review_paths[:1]
        assert len(following.state.accepted_documents) == 3
        assert services.resume().verdict is Verdict.GREEN


@pytest.mark.parametrize("fault", ["head", "hash", "document", "candidate", "path"])
def test_admission_rejects_stale_or_unreconstructible_candidate_before_models(fault):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    row = services.rows[saved.continuation_id]
    state = json.loads(row["state"])
    if fault == "head":
        services.branch_head = "f" * 40
    elif fault == "hash":
        state["candidate_sha256"] = "0" * 64
    elif fault == "document":
        state["accepted_documents"][EN + "a.md"] = "# Wrong\n"
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
            "repair": ["critic"],
            "publish": ["critic", "arbiter"],
            "report": ["critic", "arbiter"],
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
    assert services.roles == ["critic"]
    assert "report" not in services.timeline and "commit" not in services.timeline
    assert services.rows[saved.continuation_id]["status"] == "open"


def test_invalid_critic_edit_fails_closed_without_arbiter():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    before = dict(services.files)
    services.outcomes = {EN + "b.md": ["repair", "red"]}
    services.repair_payload = json.dumps({})
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == ["critic"]
    assert services.files == before and services.commits == services.initial_commits
    assert services.checkpoint().state.accepted_documents == saved.state.accepted_documents


def test_byte_identical_selected_repair_reports_existing_sha_without_empty_commit():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.outcomes = {EN + "b.md": ["repair", "green"]}
    services.repair_uses_current_values = True
    result = services.resume()
    assert result.verdict is Verdict.GREEN and not result.repair_applied
    assert result.final_commit_sha == saved.target_sha
    assert services.roles == ["critic", "arbiter"]
    assert services.commits == services.initial_commits
    assert services.timeline == ["critic", "arbiter", "report"]
    assert services.rows[saved.continuation_id]["status"] == "closed"


def test_head_movement_while_loading_comment_blocks_stale_verdict_and_close():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.move_during_report = True
    with pytest.raises(application.WorkflowError, match="report"):
        services.resume()
    assert services.roles == ["critic", "arbiter"]
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
    expected = ["critic", "arbiter"]
    assert services.roles == expected and services.timeline == [*expected, "report"]
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
    icon = "🟢" if verdict == "green" else "🔴"
    assert services.comments[0]["body"].startswith(f"{icon} {verdict.upper()}\n")
    # Restoring the exact saved head makes a later run valid. It updates that same comment.
    services.change_head = False
    services.branch_head = saved.target_sha.value
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.jobs[result.job_id]["status"] == "succeeded"
    assert services.rows[saved.continuation_id]["status"] == "closed"
    assert len(services.comments) == 1 and services.comments[0]["id"] == comment_id
    assert services.comments[0]["body"].startswith("🟢 GREEN\n")
    assert services.roles == [
        "critic", "arbiter", "critic", "arbiter"
    ]


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
    assert services.roles == ["critic", "arbiter"]
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
    assert services.roles == ["direction", "translate", "translate", "critic", "arbiter"]
    assert (
        sum(method == "POST" and path.endswith("/git/refs") for method, path in services.events)
        == 1
    )


def test_green_review_reports_green_independently_of_ci_and_closes_checkpoint():
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    services.waiting_ci = True
    result = services.resume()
    assert result.verdict is Verdict.GREEN
    assert services.comments[0]["body"].startswith("🟢 GREEN\n")
    assert "build-docs" not in services.comments[0]["body"]
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
    assert services.roles == ["critic", "arbiter"]


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
    assert eligible.state.accepted_documents == saved.state.accepted_documents
    assert services.roles == ["critic", "arbiter"]
    assert services.resume().verdict is Verdict.GREEN
    with pytest.raises(application.WorkflowError):
        services.resume()
    assert services.roles == [
        "critic", "arbiter", "critic", "arbiter"
    ]
