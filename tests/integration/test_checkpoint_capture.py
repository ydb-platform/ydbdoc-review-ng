"""Semantic stops through the shipped composition, using only external I/O fakes."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from _runtime_services import (
    RuntimeServices,
    raw_repair_context,
    rewrite_markdown,
    translated_markdown,
)

from ydbdoc_review_ng.application import TranslateWorkflowInput, VerifyWorkflowInput, WorkflowError
from ydbdoc_review_ng.continuation import (
    ContinuationStage,
    ContinuationStateError,
    candidate_sha256,
)
from ydbdoc_review_ng.domain import GitSha, RepoPath
from ydbdoc_review_ng.models import HttpResponse
from ydbdoc_review_ng.persistence import PersistenceError, YdbPersistence, semantic_stop_error
from ydbdoc_review_ng.quality import Verdict
from ydbdoc_review_ng.runtime import RuntimeSource, create_runtime
from ydbdoc_review_ng.runtime_content import RuntimeContent, pack
from ydbdoc_review_ng.runtime_continue import replay_continue
from ydbdoc_review_ng.runtime_github import GitHubBackend

ENV = {
    "GITHUB_ACTOR": "maintainer",
    "YDBDOC_ALLOWED_ACTORS": "maintainer",
    "YANDEX_API_KEY": "secret",
    "YANDEX_FOLDER_ID": "folder",
}


class CaptureServices(RuntimeServices):
    def __init__(self, names=("a", "b", "c"), *, stop=None, failure=None):
        super().__init__()
        self.names, self.stop, self.failure = names, stop, failure
        self.files = {
            f"ydb/docs/{locale}/core/{name}.md": f"# {text} {name}\n".encode()
            for name in names
            for locale, text in (("ru", "Source"), ("en", "Old"))
        }
        self.changes = [
            {"status": "modified", "filename": f"ydb/docs/ru/core/{name}.md"} for name in names
        ]
        if stop == "direction":
            self.changes.append({"status": "modified", "filename": "ydb/docs/en/core/a.md"})
        self.snapshots = {self.source: dict(self.files), self.base: dict(self.files)}
        self.rows = {}
        self.jobs = {}
        self.blobs = {}
        self.tree = []
        self.commits = 0
        self.roles = []
        self.translations = 0
        self.critics = 0
        self.saved_after_comment = False

    def execute(self, statement, parameters):
        if "a.target_path AS target_path" in statement:
            return super().execute(statement, parameters)
        if "/jobs`" in statement:
            if "SELECT" in statement:
                row = self.jobs.get(parameters["job_id"])
                return [] if row is None else [row]
            self.jobs.setdefault(parameters["job_id"], {}).update(parameters)
        if "/continuations`" in statement:
            if "UPSERT" in statement:
                self.saved_after_comment = bool(self.comments)
                self.rows[parameters["continuation_id"]] = dict(parameters)
                self.events.append(("CHECKPOINT", parameters["stage"]))
                if self.failure == "checkpoint":
                    raise OSError("write acknowledgement lost")
            elif "UPDATE" in statement:
                if parameters["continuation_id"] in self.rows:
                    row = self.rows[parameters["continuation_id"]]
                    if "SET consumed_by_job_id" in statement:
                        if all(
                            row.get(name) == value
                            for name, value in parameters.items()
                            if name != "new_consumed_by_job_id"
                        ):
                            row["consumed_by_job_id"] = parameters["new_consumed_by_job_id"]
                    elif "SET status = 'open'" in statement:
                        if all(row.get(name) == value for name, value in parameters.items()):
                            row["status"] = "open"
                    else:
                        row["status"] = "closed"
            elif "continuation_id" in parameters:
                row = self.rows.get(parameters["continuation_id"])
                return [] if row is None else [row]
            else:
                return list(self.rows.values())
            return []
        if self.failure == "attempt" and "attempt_id" in parameters:
            raise OSError("attempt persistence failed")
        if self.failure == "terminal" and "finished_at" in parameters and "role" not in parameters:
            self.failure = None
            raise OSError("terminal persistence failed")
        return super().execute(statement, parameters)

    def github(self, method, path, payload):
        relative = path.removeprefix("/repos/ydb-platform/ydb")
        if self.failure == "report" and method == "POST" and "/comments" in relative:
            raise OSError("report failed")
        if self.failure == "publish" and relative == "/git/commits" and method == "POST":
            raise OSError("publication failed")
        if relative == "/pulls/42":
            return {
                **super().github(method, path, payload),
                "changed_files": len(self.changes),
                "body": "",
                "number": 42,
            }
        if relative == "/pulls/42/files?per_page=100":
            return self.changes
        if relative.startswith("/contents/"):
            if self.failure == "validation" and self.translations == len(self.names):
                raise OSError("validation input unavailable")
            name, ref = relative[10:].split("?ref=", 1)
            content = self.snapshots[ref].get(name)
            return (
                None
                if content is None
                else {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                }
            )
        if relative == "/git/blobs":
            sha = f"{len(self.blobs) + 100:040x}"
            self.blobs[sha] = base64.b64decode(payload["content"])
            return {"sha": sha}
        if relative == "/git/trees":
            self.tree = payload["tree"]
            return {"sha": "d" * 40}
        if relative == "/git/commits" and method == "POST":
            self.commits += 1
            self.translated = f"{self.commits + 200:040x}"
            files = dict(self.snapshots[self.branch_head or self.base])
            for item in self.tree:
                if item["sha"] is None:
                    files.pop(item["path"], None)
                else:
                    files[item["path"]] = self.blobs[item["sha"]]
            self.snapshots[self.translated] = files
            return {"sha": self.translated}
        if relative == "/git/refs" or relative.startswith("/git/refs/heads/"):
            self.events.append((method, path))
            self.branch_head = payload["sha"]
            self.files = dict(self.snapshots[self.branch_head])
            return {}
        if relative == "/issues/42/comments" and method == "POST":
            self.comments.append({"id": 8, "body": payload["body"]})
            return {"id": 8}
        result = super().github(method, path, payload)
        if self.failure == "head" and method == "POST" and relative == "/issues/43/comments":
            self.branch_head = "f" * 40
        if self.failure == "candidate" and method == "POST" and relative == "/issues/43/comments":
            self.snapshots[self.branch_head]["ydb/docs/en/core/a.md"] = b"# Unpublished\n"
        return result

    def model(self, request):
        body = json.loads(request.body)
        prompt = body["messages"][-1]["text"]
        schema_wrapper = body.get("jsonSchema")
        if schema_wrapper is None:
            if prompt.startswith("Repair"):
                role = "repair"
                text = rewrite_markdown(
                    raw_repair_context(prompt, "current-target"), "Corrected"
                )
            else:
                role = "translate"
                self.translations += 1
                text = translated_markdown(prompt)
                if self.stop == "translation" and self.translations in {2, 3}:
                    text = "[[YDBDOC_PROTECTED_9999]]"
                if self.stop == "translation_assembly" and self.translations in {2, 3}:
                    text = "[[YDBDOC_PROTECTED_9999]]"
        elif "verdict" in (schema := schema_wrapper["schema"])["properties"]:
            role = "critic"
            self.critics += 1
            props = schema["properties"]["findings"]["items"]["properties"]
            path = props["target_path"]["const"]
            red = (
                self.stop == "rename_red"
                or self.stop == "review"
                and (path.endswith("/b.md") or self.critics == 1)
            )
            values = {"verdict": "RED" if red else "GREEN", "findings": []}
            if red:
                values["findings"] = [
                    {
                        "repairable": self.stop != "rename_red" and self.critics == 1,
                        "reason": "The meaning is incomplete.",
                        "expected_correction": "Restore the missing meaning.",
                        "searchable_snippet": "Translated",
                        "target_path": path,
                        "target_line": 1,
                    }
                ]
                if values["findings"][0]["repairable"]:
                    values["findings"][0]["field_ids"] = props["field_ids"]["items"]["enum"]
        elif prompt.startswith("Compare"):
            role = "direction"
            values = dict.fromkeys(schema["properties"], "undetermined")
        else:
            raise AssertionError("unexpected structured model role")
        self.roles.append(role)
        if self.failure == role or self.failure == "final_critic" and self.critics == 2:
            raise TimeoutError("transport failed")
        if role not in {"translate", "repair"}:
            text = json.dumps(values)
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
                                    "text": text,
                                },
                            }
                        ],
                        "usage": {"inputTextTokens": "10", "completionTokens": "5"},
                    }
                }
            ).encode(),
            Decimal("0.01"),
        )

    def runtime(self):
        return create_runtime(
            environment=ENV,
            ydb_executor=self,
            github_transport=self.github,
            model_transport=self.model,
        )

    def translate(self):
        return self.runtime().doc_translate(
            TranslateWorkflowInput(42, GitSha(self.source), Decimal(10))
        )

    def checkpoint(self):
        return YdbPersistence(self).load_checkpoint(42, now=datetime.now(UTC))


def test_direction_stop_is_strict_and_warns_before_saving():
    services = CaptureServices(stop="direction")
    with pytest.raises(WorkflowError):
        services.translate()
    checkpoint = services.checkpoint()
    assert checkpoint.state.stage is ContinuationStage.DIRECTION
    assert checkpoint.state.direction is checkpoint.state.scope_sha256 is None
    assert checkpoint.scope_target_paths == ()
    assert len(checkpoint.source_inventory.files) == 4
    assert checkpoint.source_sha == GitSha(services.source)
    assert checkpoint.base_sha == GitSha(services.base)
    assert services.saved_after_comment and services.roles == ["direction"]
    assert services.audit[-1]["status"] == "failed"


@pytest.mark.parametrize("stop", ["translation", "translation_assembly"])
def test_two_invalid_current_field_responses_preserve_first_map_and_pending_order(stop):
    services = CaptureServices(stop=stop)
    services.changes.append({"status": "removed", "filename": "ydb/docs/ru/core/z.md"})
    for files in [services.files, *services.snapshots.values()]:
        files["ydb/docs/en/core/z.md"] = b"# Remove this counterpart\n"
    with pytest.raises(WorkflowError):
        services.translate()
    checkpoint = services.checkpoint()
    assert checkpoint.state.stage is ContinuationStage.TRANSLATION
    assert [item.target_path.value for item in checkpoint.state.accepted_documents] == [
        "ydb/docs/en/core/a.md"
    ]
    assert checkpoint.state.accepted_documents[0].translated_markdown == "# Translated\n"
    assert checkpoint.state.pending_paths == tuple(
        RepoPath(f"ydb/docs/en/core/{n}.md") for n in ("b", "c")
    )
    assert checkpoint.scope_target_paths == tuple(
        RepoPath(f"ydb/docs/en/core/{n}.md") for n in ("a", "b", "c", "z")
    )
    assert services.roles == ["translate", "translate", "translate"]
    attempts = [row for row in services.audit if "attempt_id" in row]
    assert len(attempts) == 3
    assert sum(row["cost_rub"] for row in attempts) == Decimal("0.03")
    assert services.commits == 0 and services.audit[-1]["status"] == "failed"


@pytest.mark.parametrize("mode", ["translate", "verify"])
def test_review_red_saves_final_repair_map_exact_published_candidate_and_unresolved_paths(mode):
    services = CaptureServices(stop="review")
    if mode == "verify":
        services.branch_head = services.translated
        services.snapshots[services.translated] = {
            p: b"# Translated\n" if "/en/" in p else content
            for p, content in services.files.items()
        }
        result = services.runtime().doc_verify(
            VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
        )
    else:
        result = services.translate()
    checkpoint = services.checkpoint()
    assert result.verdict is Verdict.RED and result.repair_applied
    assert checkpoint.state.stage is ContinuationStage.REVIEW
    assert checkpoint.target_sha == result.final_commit_sha == GitSha(services.branch_head)
    assert checkpoint.trigger_pr == 43
    assert checkpoint.state.review_paths == (RepoPath("ydb/docs/en/core/b.md"),)
    assert checkpoint.state.accepted_documents[0].translated_markdown == "# Corrected\n"
    candidate = pack(
        {p: services.files[p] for p in [f"ydb/docs/en/core/{n}.md" for n in services.names]}
    )
    assert checkpoint.state.candidate_sha256 == candidate_sha256(candidate)
    assert services.saved_after_comment and services.comments[-1]["body"].startswith("RED\n")
    assert services.audit[-1]["status"] in {"succeeded", "failed"}


def test_green_does_not_open_checkpoint():
    services = CaptureServices()
    assert services.translate().verdict is Verdict.GREEN
    assert services.rows == {}


def test_provider_non_final_translation_is_rejected_before_publication():
    class NonFinalServices(CaptureServices):
        def model(self, request):
            response = super().model(request)
            payload = json.loads(response.body)
            payload["result"]["alternatives"][0]["status"] = (
                "ALTERNATIVE_STATUS_TRUNCATED_FINAL"
            )
            return HttpResponse(200, json.dumps(payload).encode(), Decimal("0.01"))

    services = NonFinalServices(names=("a",))

    with pytest.raises(WorkflowError):
        services.translate()

    attempts = [row for row in services.audit if "attempt_id" in row]
    assert services.roles == ["translate"]
    assert len(attempts) == 1 and attempts[0]["error"] == "non_final"
    assert services.commits == 0 and services.blobs == {} and services.tree == []


def test_red_pure_rename_replays_whole_counterpart_as_a_complete_document():
    services = CaptureServices(names=("a",), stop="rename_red")
    services.changes = [
        {
            "status": "renamed",
            "filename": "ydb/docs/ru/core/a.md",
            "previous_filename": "ydb/docs/ru/core/old.md",
            "changes": 0,
        }
    ]
    for files in [services.files, *services.snapshots.values()]:
        files["ydb/docs/en/core/old.md"] = files.pop("ydb/docs/en/core/a.md")
    assert services.translate().verdict is Verdict.RED
    checkpoint = services.checkpoint()
    assert checkpoint.state.accepted_documents[0].translated_markdown == "# Old a\n"
    assert checkpoint.state.review_paths == (RepoPath("ydb/docs/en/core/a.md"),)
    source = RuntimeSource(ENV, GitHubBackend(services.github))
    content = RuntimeContent(source, None, ENV)
    replay = replay_continue(content, checkpoint)
    assert replay.accepted_documents == checkpoint.state.accepted_documents
    assert services.roles == ["critic"]
    with pytest.raises(PersistenceError, match="scope selection"):
        replay_continue(
            content,
            replace(
                checkpoint,
                state=replace(
                    checkpoint.state, review_paths=(RepoPath("ydb/docs/en/core/outside.md"),)
                ),
            ),
        )


def test_verify_red_rename_keeps_replayable_metadata_candidate():
    services = CaptureServices(names=("a",), stop="rename_red")
    services.changes = [
        {
            "status": "renamed",
            "filename": "ydb/docs/ru/core/a.md",
            "previous_filename": "ydb/docs/ru/core/old.md",
            "changes": 0,
        }
    ]
    for files in [services.files, *services.snapshots.values()]:
        files["ydb/docs/en/core/old.md"] = files.pop("ydb/docs/en/core/a.md")
    services.translate()
    services.rows.clear()
    result = services.runtime().doc_verify(
        VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
    )
    assert result.verdict is Verdict.RED
    checkpoint = services.checkpoint()
    content = RuntimeContent(RuntimeSource(ENV, GitHubBackend(services.github)), None, ENV)
    services.snapshots[services.branch_head]["ydb/docs/en/redirects.yaml"] = (
        b"POISONED TARGET METADATA"
    )
    replay = replay_continue(content, checkpoint)
    rebuilt = content.assemble_documents(
        replay.plans, replay.accepted_documents, replay.accepted_maps
    )
    assert candidate_sha256(rebuilt.content) == checkpoint.state.candidate_sha256
    assert b"POISONED" not in rebuilt.content


def test_successfully_repaired_pure_rename_replays_the_new_map():
    services = CaptureServices(names=("a", "b"), stop="review")
    services.changes[0] = {
        "status": "renamed",
        "filename": "ydb/docs/ru/core/a.md",
        "previous_filename": "ydb/docs/ru/core/old.md",
        "changes": 0,
    }
    for files in [services.files, *services.snapshots.values()]:
        files.pop("ydb/docs/en/core/a.md")
        files["ydb/docs/en/core/old.md"] = b"# Translated\n"
    result = services.translate()
    assert result.verdict is Verdict.RED and result.repair_applied
    checkpoint = services.checkpoint()
    assert checkpoint.state.accepted_documents[0].translated_markdown == "# Corrected\n"
    content = RuntimeContent(RuntimeSource(ENV, GitHubBackend(services.github)), None, ENV)
    replay = replay_continue(content, checkpoint)
    assert (
        candidate_sha256(
            content.assemble_documents(
                replay.plans, replay.accepted_documents, replay.accepted_maps
            ).content
        )
        == checkpoint.state.candidate_sha256
    )


@pytest.mark.parametrize("mode", ["translate", "verify"])
def test_existing_rename_branch_checkpoint_replays_the_exact_current_candidate(mode):
    services = CaptureServices(names=("a",), stop="rename_red")
    services.changes = [
        {
            "status": "renamed",
            "filename": "ydb/docs/ru/core/a.md",
            "previous_filename": "ydb/docs/ru/core/old.md",
            "changes": 0,
        }
    ]
    for files in [services.files, *services.snapshots.values()]:
        files["ydb/docs/en/core/old.md"] = files.pop("ydb/docs/en/core/a.md")
    services.translate()
    services.rows.clear()
    if mode == "verify":
        services.snapshots[services.branch_head]["ydb/docs/en/core/a.md"] = b"# Human correction\n"
        services.runtime().doc_verify(
            VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
        )
    else:
        # A redo also changes a document, so the current RED is reported on the PR.
        services.snapshots[services.branch_head]["ydb/docs/en/core/a.md"] = (
            b"# Previous candidate\n"
        )
        services.translate()
    checkpoint = services.checkpoint()
    content = RuntimeContent(RuntimeSource(ENV, GitHubBackend(services.github)), None, ENV)
    replay_continue(content, checkpoint)


def test_review_path_cannot_name_a_selected_delete_operation():
    services = CaptureServices(names=("a",), stop="rename_red")
    services.changes.append({"status": "removed", "filename": "ydb/docs/ru/core/deleted.md"})
    for files in [services.files, *services.snapshots.values()]:
        files["ydb/docs/en/core/deleted.md"] = b"# Gone\n"
    services.translate()
    checkpoint = services.checkpoint()
    deleted = RepoPath("ydb/docs/en/core/deleted.md")
    assert deleted in checkpoint.scope_target_paths
    corrupted = replace(checkpoint, state=replace(checkpoint.state, review_paths=(deleted,)))
    content = RuntimeContent(RuntimeSource(ENV, GitHubBackend(services.github)), None, ENV)
    with pytest.raises(ContinuationStateError):
        replay_continue(content, corrupted)


def test_red_without_a_published_current_verdict_never_opens_checkpoint():
    services = CaptureServices(names=("a",), stop="rename_red")
    services.branch_head = services.translated
    services.snapshots[services.translated] = dict(services.files)
    for files in [services.files, *services.snapshots.values()]:
        files["ydb/docs/en/core/a.md"] = b"# Translated\n"
    with pytest.raises(WorkflowError):
        services.translate()
    assert services.comments == []
    assert services.rows == {}


@pytest.mark.parametrize(
    "failure",
    [
        "direction",
        "translate",
        "critic",
        "final_critic",
        "repair",
        "attempt",
        "validation",
        "publish",
        "report",
        "head",
        "candidate",
        "checkpoint",
        "terminal",
    ],
)
def test_infrastructure_failure_does_not_leave_open_checkpoint(failure):
    services = CaptureServices(
        stop="direction" if failure == "direction" else "review", failure=failure
    )
    with pytest.raises(WorkflowError):
        services.translate()
    assert not any(row["status"] == "open" for row in services.rows.values())
    assert services.audit[-1]["status"] == "failed"
    if failure == "repair":
        assert services.roles[-1] == "repair"


def test_lost_terminal_ack_and_failed_close_cannot_be_resumed():
    class LostTerminalAck(CaptureServices):
        def execute(self, statement, parameters):
            if "/continuations`" in statement and "UPDATE" in statement:
                raise OSError("database unavailable before compensating close")
            result = super().execute(statement, parameters)
            if "finished_at" in parameters and "role" not in parameters:
                raise OSError("terminal committed, acknowledgement lost")
            return result

    services = LostTerminalAck(stop="translation")
    with pytest.raises((WorkflowError, PersistenceError)):
        services.translate()
    assert services.roles == ["translate", "translate", "translate"]
    assert services.rows  # The real checkpoint write reached storage.
    with pytest.raises(PersistenceError):
        services.checkpoint()
    assert next(iter(services.rows.values()))["status"] == "pending"
    assert next(iter(services.jobs.values()))["error"] == "terminal_audit_failed"


@pytest.mark.parametrize("stop", ["direction", "translation", "review"])
@pytest.mark.parametrize("activation_failure", ["before", "after", "readback"])
def test_activation_ambiguity_requires_exact_readback_and_semantic_job(stop, activation_failure):
    class ActivationServices(CaptureServices):
        activated = False

        def execute(self, statement, parameters):
            if "SET status = 'open'" in statement:
                row = self.rows[parameters["continuation_id"]]
                assert row["status"] == "pending"
                assert self.jobs[row["job_id"]]["error"] == "continuable_" + row["stage"]
                if activation_failure == "before":
                    raise OSError("activation did not reach storage")
                result = super().execute(statement, parameters)
                self.activated = True
                if activation_failure == "after":
                    raise OSError("activation acknowledgement lost")
                return result
            if (
                self.activated
                and activation_failure == "readback"
                and "/continuations`" in statement
                and "SELECT" in statement
            ):
                self.activated = False
                raise OSError("activation cannot be confirmed")
            return super().execute(statement, parameters)

    services = ActivationServices(stop=stop)
    if stop == "review" and activation_failure == "after":
        assert services.translate().verdict is Verdict.RED
    else:
        with pytest.raises(WorkflowError):
            services.translate()
    if activation_failure == "after":
        checkpoint = services.checkpoint()
        assert checkpoint.status.value == "open"
        assert services.jobs[checkpoint.job_id]["error"] == semantic_stop_error(
            checkpoint.state.stage
        )
        assert checkpoint.created_at == services.jobs[checkpoint.job_id]["started_at"]
    else:
        with pytest.raises(PersistenceError):
            services.checkpoint()
        assert next(iter(services.jobs.values()))["error"] == "checkpoint_failed"


def test_pending_remains_non_resumable_when_both_cleanup_writes_are_unavailable():
    class UnavailableCleanup(CaptureServices):
        terminal_writes = 0
        closes = 0

        def execute(self, statement, parameters):
            if "/continuations`" in statement and "UPDATE" in statement:
                self.closes += 1
                raise OSError("close unavailable")
            if "finished_at" in parameters and "role" not in parameters:
                self.terminal_writes += 1
                if self.terminal_writes == 1:
                    super().execute(statement, parameters)
                raise OSError("terminal acknowledgement or infrastructure write unavailable")
            return super().execute(statement, parameters)

    services = UnavailableCleanup(stop="translation")
    with pytest.raises(WorkflowError):
        services.translate()
    assert services.closes == 1 and services.terminal_writes == 2
    assert next(iter(services.jobs.values()))["error"] == "continuable_translation"
    assert next(iter(services.rows.values()))["status"] == "pending"
    with pytest.raises(PersistenceError):
        services.checkpoint()


def test_activation_readback_rechecks_the_semantic_job_marker():
    class ChangedAudit(CaptureServices):
        def execute(self, statement, parameters):
            result = super().execute(statement, parameters)
            if "SET status = 'open'" in statement:
                self.jobs[parameters["job_id"]]["error"] = "prepare_failed"
                raise OSError("activation committed but acknowledgement lost")
            return result

    services = ChangedAudit(stop="review")
    with pytest.raises(WorkflowError):
        services.translate()
    assert next(iter(services.rows.values()))["status"] == "closed"
    with pytest.raises(PersistenceError):
        services.checkpoint()
