"""The installed production composition, with only external I/O replaced."""

from __future__ import annotations

import base64
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest
from _runtime_services import (
    InstalledContinueServices,
    RuntimeServices,
    request_prompt,
    request_schema,
    translation_segments,
)


@pytest.mark.parametrize("mode", ["translate", "verify", "continue"])
@pytest.mark.parametrize("verdict", ["GREEN", "YELLOW", "RED"])
@pytest.mark.parametrize("docs_root", ["", "/unavailable/ydb/docs"])
def test_semantic_modes_publish_without_build_or_ci(monkeypatch, mode, verdict, docs_root):
    from ydbdoc_review_ng.application import (
        ContinueWorkflowInput,
        TranslateWorkflowInput,
        VerifyWorkflowInput,
    )
    from ydbdoc_review_ng.diplodoc import DiplodocBuildValidator
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.runtime import create_runtime

    class Services(InstalledContinueServices):
        def github(self, method, path, payload):
            if method == "POST" and path.endswith("/git/commits"):
                self.translated = f"{int(self.translated, 16) + 1:040x}"
            return super().github(method, path, payload)

    services = Services()
    environment = {
        "GITHUB_ACTOR": "maintainer",
        "YDBDOC_ALLOWED_ACTORS": "maintainer",
        "YANDEX_API_KEY": "offline",
        "YANDEX_FOLDER_ID": "offline",
    }
    finding = {
        "reason": "Residual meaning issue.",
        "expected_correction": "Restore the intended meaning.",
        "searchable_snippet": "Corrected",
        "target_path": "ydb/docs/en/core/page.md",
        "target_line": 1,
    }

    def runtime():
        return create_runtime(
            environment=environment,
            ydb_executor=services,
            github_transport=github_without_ci,
            model_transport=services.model,
        )

    def github_without_ci(method, path, payload):
        assert not any(
            part in path for part in ("/check-runs", "/check-suites", "/status", "/actions/")
        ), f"semantic workflow read CI state: {path}"
        return services.github(method, path, payload)

    if mode != "translate":
        services.branch_head = services.translated
        services.pr_exists = True
    if mode == "continue":
        services.semantic_responses = [
            {"files": {"ydb/docs/en/core/page.md": "# Corrected\n"}},
            {"verdict": "RED", "findings": [finding]},
        ]
        seed = runtime().doc_verify(
            VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
        )
        assert seed.verdict.value == "RED"
        assert any(row["status"] == "open" for row in services.checkpoints.values())
        services.continuing = True

    def forbidden_build(*args, **kwargs):
        raise AssertionError("semantic workflow accessed the Diplodoc builder")

    for name in ("__init__", "validate_baseline", "__call__"):
        monkeypatch.setattr(DiplodocBuildValidator, name, forbidden_build)
    environment["YDBDOC_DOCS_ROOT"] = docs_root
    services.events.clear()
    services.semantic_responses = [
        {"files": {"ydb/docs/en/core/page.md": "# Corrected final\n"}},
        {"verdict": verdict, "findings": [] if verdict == "GREEN" else [finding]},
    ]
    dispatcher = runtime()
    if mode == "translate":
        result = dispatcher.doc_translate(
            TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
        )
    elif mode == "verify":
        result = dispatcher.doc_verify(
            VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
        )
    else:
        result = dispatcher.doc_continue(ContinueWorkflowInput(43))

    assert result.verdict.value == verdict
    assert services.files["ydb/docs/en/core/page.md"] == b"# Corrected final\n"
    assert any(
        method in {"POST", "PATCH"} and "/git/refs" in path
        for method, path in services.events
    )
    assert len(services.comments) == 1
    assert verdict in services.comments[0]["body"]
    assert not services.semantic_responses


class WholePRServices(InstalledContinueServices):
    """Pinned HTTP snapshots and a literal two-response semantic script."""

    def __init__(self, verdict="GREEN", check_conclusion="success"):
        super().__init__()
        self.branch_head = self.translated
        self.pr_exists = True
        self.check_conclusion = check_conclusion
        self.requests = []
        self.blobs = {}
        self.published = {}
        self.source_files = {
            "ydb/docs/ru/core/a.md": "# Source A\n\nCurrent alpha.\n\nUnchanged A.\n",
            "ydb/docs/ru/core/b.md": "# Source B\n\nCurrent alpha relationship.\n\nUnchanged B.\n",
            "ydb/docs/ru/core/toc.yaml": (
                "items:\n  - name: Source A\n    href: a.md\n  - name: Source B\n    href: b.md\n"
            ),
        }
        self.target_files = {
            "ydb/docs/en/core/a.md": "# Draft A\n\nDraft alpha.\n\nUnchanged A.\n",
            "ydb/docs/en/core/b.md": "# Draft B\n\nDraft alpha relationship.\n\nUnchanged B.\n",
            "ydb/docs/en/core/toc.yaml": (
                "items:\n  - name: Draft A\n    href: a.md\n"
                "  - name: Draft B\n    href: b.md\n"
                "  - name: Target only\n    href: target-only.md\n"
            ),
        }
        self.corrected_files = {
            "ydb/docs/en/core/a.md": "# Corrected A\n\nCorrect alpha.\n\nUnchanged A.\n",
            "ydb/docs/en/core/b.md": (
                "# Corrected B\n\nCorrect alpha relationship.\n\nUnchanged B.\n"
            ),
            "ydb/docs/en/core/toc.yaml": (
                "items:\n  - name: Draft A\n    href: a.md\n"
                "  - name: Draft B\n    href: b.md\n"
                "  - name: Target only\n    href: target-only.md\n"
            ),
        }
        self.glossary = {
            "ydb/docs/ru/core/concepts/glossary.md": (
                "# Glossary RU\n" + "Full definition.\n" * 900 + "Tail alpha definition.\n"
            ),
            "ydb/docs/en/core/concepts/glossary.md": "# Glossary EN\nAlpha definition.\n",
        }
        self.files = {
            path: content.encode()
            for path, content in (self.source_files | self.target_files | self.glossary).items()
        }
        self.semantic_responses = [
            {"files": dict(self.corrected_files)},
            {
                "verdict": verdict,
                "findings": []
                if verdict == "GREEN"
                else [
                    {
                        "reason": "Residual alpha detail.",
                        "expected_correction": "Clarify alpha detail.",
                        "searchable_snippet": "Correct alpha.",
                        "target_path": "ydb/docs/en/core/a.md",
                        "target_line": 3,
                    },
                    {
                        "reason": "Residual relationship detail.",
                        "expected_correction": "Clarify relationship detail.",
                        "searchable_snippet": "Correct alpha relationship.",
                        "target_path": "ydb/docs/en/core/b.md",
                        "target_line": 3,
                    },
                ],
            },
        ]

    def github(self, method, path, payload):
        relative = path.removeprefix("/repos/ydb-platform/ydb")
        if relative == "/pulls/42/files?per_page=100":
            return [{"status": "modified", "filename": name} for name in self.source_files]
        if relative.startswith("/contents/") and relative.endswith("?ref=" + self.base):
            name = relative[10:].split("?", 1)[0]
            if name == "ydb/docs/ru/core/toc.yaml":
                return {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(b"items:\n").decode(),
                }
            if name in self.source_files and name.endswith(".md"):
                return {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(b"# Preimage\n\nObsolete detail.\n").decode(),
                }
        if relative == "/git/blobs":
            sha = f"{len(self.blobs) + 1:040x}"
            self.blobs[sha] = base64.b64decode(payload["content"])
            return {"sha": sha}
        if relative == "/git/trees":
            self.published = {item["path"]: self.blobs[item["sha"]] for item in payload["tree"]}
            return {"sha": "d" * 40}
        if relative == "/git/commits" and method == "POST":
            self.translated = "f" * 40
            return {"sha": self.translated}
        if relative.startswith("/git/refs"):
            self.events.append((method, path))
            self.files.update(self.published)
            self.branch_head = payload["sha"]
            return {}
        result = super().github(method, path, payload)
        if relative == "/pulls/42":
            result["changed_files"] = 3
        if "/check-runs?" in relative:
            for check in result["check_runs"]:
                check["conclusion"] = self.check_conclusion
        return result

    def model(self, request):
        body = json.loads(request.body)
        self.requests.append((request_prompt(body), request_schema(body)))
        return super().model(request)

    def verify(self):
        from ydbdoc_review_ng.application import VerifyWorkflowInput
        from ydbdoc_review_ng.domain import GitSha
        from ydbdoc_review_ng.runtime import create_runtime

        runtime = create_runtime(
            environment={
                "GITHUB_ACTOR": "m",
                "YDBDOC_ALLOWED_ACTORS": "m",
                "YANDEX_API_KEY": "offline",
                "YANDEX_FOLDER_ID": "offline",
            },
            ydb_executor=self,
            github_transport=self.github,
            model_transport=self.model,
        )
        return runtime.doc_verify(
            VerifyWorkflowInput(
                43,
                GitSha(self.source),
                GitSha(self.translated),
            )
        )


def test_runtime_full_pr_critic_and_arbiter_share_exact_complete_inputs():
    from _runtime_services import raw_repair_context

    services = WholePRServices()
    result = services.verify()
    assert result.verdict.value == "GREEN"
    assert len(services.requests) == 2 and not services.semantic_responses
    for index, (prompt, schema) in enumerate(services.requests):
        assert json.loads(raw_repair_context(prompt, "source-pr-files")) == services.source_files
        assert json.loads(raw_repair_context(prompt, "translation-pr-files")) == (
            services.target_files if index == 0 else services.corrected_files
        )
        assert json.loads(raw_repair_context(prompt, "project-glossary")) == services.glossary
        assert set(schema["schema"]["properties"]) == (
            {"files"} if index == 0 else {"verdict", "findings"}
        )
    assert services.published == {
        path: text.encode()
        for path, text in services.corrected_files.items()
        if path.endswith(".md")
    }
    assert len(services.comments) == 1


@pytest.mark.parametrize("verdict", ["YELLOW", "RED"])
def test_runtime_residual_findings_report_once_without_further_model_calls(verdict):
    services = WholePRServices(verdict)
    result = services.verify()
    assert result.verdict.value == verdict
    assert len(services.requests) == 2 and not services.semantic_responses
    assert len(services.comments) == 1
    report = services.comments[0]["body"]
    assert verdict in report
    for text in ("Residual alpha detail.", "Residual relationship detail."):
        assert report.count(text) == 1
        assert all(text not in prompt for prompt, _schema in services.requests)
    assert ("doc_continue" in report) == (verdict == "RED")


@pytest.mark.parametrize("verdict", ["GREEN", "YELLOW", "RED"])
@pytest.mark.parametrize("check_conclusion", ["success", "failure", None])
def test_repository_checks_do_not_change_semantic_verdict(verdict, check_conclusion):
    services = WholePRServices(verdict, check_conclusion)
    result = services.verify()
    assert result.verdict.value == verdict
    assert verdict in services.comments[0]["body"]
    assert len(services.requests) == 2
    assert all("build-docs" not in prompt for prompt, _schema in services.requests)


def test_merged_source_uses_workflow_pinned_base_after_branch_advances() -> None:
    from ydbdoc_review_ng.application import TranslateWorkflowInput
    from ydbdoc_review_ng.domain import GitSha, RepositoryId, SnapshotRef
    from ydbdoc_review_ng.repository import (
        BaseBranch,
        PullRequestState,
        ResolvedRepositorySnapshots,
    )
    from ydbdoc_review_ng.runtime import RuntimeSource
    from ydbdoc_review_ng.runtime_github import GitHubBackend

    class MergedServices(RuntimeServices):
        merge_commit = "c" * 40

        def github(self, method, path, payload):
            response = super().github(method, path, payload)
            if path.endswith("/pulls/42"):
                return {**response, "merged": True, "merge_commit_sha": self.merge_commit}
            return response

    services = MergedServices()
    previous_translation_head = GitSha("f" * 40)
    services.branch_head = previous_translation_head.value
    source = RuntimeSource(
        {"GITHUB_ACTOR": "maintainer", "YDBDOC_ALLOWED_ACTORS": "maintainer"},
        GitHubBackend(services.github),
    )
    request = TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    authorization = source.authorize_translate(request)

    snapshot = source.snapshot_translate(authorization)

    repository = RepositoryId("ydb-platform/ydb")
    pinned_base = SnapshotRef(repository, GitSha(services.source))
    provenance = SnapshotRef(repository, GitSha(services.merge_commit))
    assert snapshot.source_sha == pinned_base.commit_sha
    assert source.snapshots == ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        provenance,
        pinned_base,
        pinned_base,
        pinned_base,
        pinned_base,
        pinned_base,
        provenance,
    )
    assert source.context.current_head == pinned_base.commit_sha
    assert source.context.expected_branch_head == previous_translation_head
    assert source.inventory.source_base_sha == GitSha(services.base)
    assert source.inventory.source_head_sha == GitSha(services.source)
    assert source.source_change_snapshot.commit_sha == GitSha(services.source)
    merge_base_with = source.snapshots.merge_base_with
    assert merge_base_with is not None
    assert GitSha(services.base) not in {
        source.snapshots.source_snapshot.commit_sha,
        source.snapshots.target_snapshot.commit_sha,
        source.snapshots.scope_snapshot.commit_sha,
        source.snapshots.translation_base_snapshot.commit_sha,
        merge_base_with.commit_sha,
        source.context.current_head,
    }


def test_shipped_composition_translates_then_verifies_current_pr_without_retranslation():
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = RuntimeServices()
    environment = {
        "GITHUB_ACTOR": "maintainer",
        "YDBDOC_ALLOWED_ACTORS": "maintainer",
        "YANDEX_API_KEY": "secret",
        "YANDEX_FOLDER_ID": "folder",
    }
    model_uris = []

    def record_model(request):
        body = json.loads(request.body)
        model_uris.append(body.get("modelUri") or body.get("model"))
        return services.model(request)

    runtime = create_runtime(
        environment=environment,
        ydb_executor=services,
        github_transport=services.github,
        model_transport=record_model,
    )
    assert (
        main(
            ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
            dispatcher=runtime,
        )
        == 0
    )
    assert "deepseek" in model_uris[0]
    assert "deepseek" in model_uris[1]
    assert services.files["ydb/docs/en/core/page.md"] == b"# Translated\n"
    assert len(services.comments) == 1
    assert services.comments[0]["body"].startswith("🟢 GREEN\n")
    events = services.events
    assert (
        next(i for i, event in enumerate(events) if event == ("MODEL", ("files",)))
        < next(
            i
            for i, event in enumerate(events)
            if event == ("POST", "/repos/ydb-platform/ydb/git/refs")
        )
        < next(
            i
            for i, event in enumerate(events)
            if event == ("POST", "/repos/ydb-platform/ydb/pulls")
        )
    )
    services.events = []
    services.semantic_responses = [
        {"files": {"ydb/docs/en/core/page.md": "# Translated\n"}},
        {"verdict": "GREEN", "findings": []},
    ]
    runtime = create_runtime(
        environment=environment,
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    assert (
        main(
            [
                "verify",
                "--pr",
                "43",
                "--source-sha",
                services.source,
                "--target-sha",
                services.translated,
            ],
            dispatcher=runtime,
        )
        == 0
    )
    assert [event for event in services.events if event[0] == "MODEL"] == [
        ("MODEL", ("files",)),
        ("MODEL", ("verdict", "findings")),
    ]
    assert not any(
        "/git/" in path and method in {"POST", "PATCH"} for method, path in services.events
    )
    assert len(services.comments) == 1
    assert (
        sum(row.get("status") == "succeeded" for row in services.audit) == 8
    )  # two jobs plus six model attempts (direction, translate, critic×2, arbiter×2)


def test_runtime_publishes_field_local_inline_code_grammar_order_once() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.models import HttpResponse
    from ydbdoc_review_ng.runtime import create_runtime

    class MobilityServices(RuntimeServices):
        def __init__(self) -> None:
            super().__init__()
            self.files["ydb/docs/ru/core/page.md"] = (
                "* В системные представления `.sys/top_queries_*` и "
                "`.sys/query_sessions` добавлена колонка `TraceId`.\n"
            ).encode()
            self.raw_calls = 0
            self.semantic_responses[0] = {
                "files": {
                    "ydb/docs/en/core/page.md": (
                        "* The `TraceId` column was added to `.sys/top_queries_*` and "
                        "`.sys/query_sessions`.\n"
                    )
                }
            }

        def model(self, request):
            body = json.loads(request.body)
            schema_wrapper = request_schema(body)
            if schema_wrapper is not None:
                properties = schema_wrapper["schema"]["properties"]
                if all(key.startswith("segment_") for key in properties):
                    self.raw_calls += 1
                    return super().model(request)
                return super().model(request)
            self.raw_calls += 1
            self.events.append(("MODEL", ("raw_markdown",)))
            text = (
                "* The [[YDBDOC_PROTECTED_0003]] column was added to "
                "[[YDBDOC_PROTECTED_0001]] and [[YDBDOC_PROTECTED_0002]].\n"
            )
            return HttpResponse(
                200,
                json.dumps(
                    {
                        "result": {
                            "alternatives": [
                                {
                                    "status": "ALTERNATIVE_STATUS_FINAL",
                                    "message": {"role": "assistant", "text": text},
                                }
                            ],
                            "usage": {
                                "inputTextTokens": "10",
                                "completionTokens": "5",
                            },
                        }
                    }
                ).encode(),
                Decimal("0.01"),
            )

    services = MobilityServices()
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    exit_code = main(
        ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
        dispatcher=runtime,
    )

    assert exit_code == 0
    assert services.raw_calls == 1
    assert services.files["ydb/docs/en/core/page.md"] == (
        b"* The `TraceId` column was added to `.sys/top_queries_*` and `.sys/query_sessions`.\n"
    )
    assert (
        sum(method in {"POST", "PATCH"} and "/git/refs" in path for method, path in services.events)
        == 1
    )


def test_runtime_preserves_list_formatting_drift_through_critic() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.models import HttpResponse
    from ydbdoc_review_ng.runtime import create_runtime

    class FormattingServices(RuntimeServices):
        def __init__(self) -> None:
            super().__init__()
            self.files["ydb/docs/ru/core/page.md"] = (
                "* Parent\n  * `enable_strict_user_management` — исходный пункт "
                "(то есть только администратор)\n"
            ).encode()
            self.raw_calls = 0
            self.critic_calls = 0
            self.semantic_responses[0] = {
                "files": {
                    "ydb/docs/en/core/page.md": (
                        "* Parent translated\n"
                        "* `enable_strict_user_management` — corrected item "
                        "(i.e., only an administrator)\n"
                    )
                }
            }

        def model(self, request):
            body = json.loads(request.body)
            schema_wrapper = request_schema(body)
            if schema_wrapper is not None:
                properties = schema_wrapper["schema"]["properties"]
                if all(key.startswith("segment_") for key in properties):
                    self.raw_calls += 1
                    return super().model(request)
                self.critic_calls += 1
                return super().model(request)
            else:
                self.raw_calls += 1
                prompt = request_prompt(body)
                self.events.append(
                    ("REPAIR", "markdown")
                    if prompt.startswith("Repair")
                    else ("MODEL", ("raw_markdown",))
                )
                text = (
                    "* Parent translated\n"
                    "* [[YDBDOC_PROTECTED_0001]] — corrected item "
                    "(i.e., only an administrator)\n"
                    if prompt.startswith("Repair")
                    else (
                        "* Parent translated\n"
                        "* [[YDBDOC_PROTECTED_0001]] — translated item "
                        "(i.e., only an administrator)\n"
                    )
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
                                        "text": text,
                                    },
                                }
                            ],
                            "usage": {
                                "inputTextTokens": "10",
                                "completionTokens": "5",
                            },
                        }
                    }
                ).encode(),
                Decimal("0.01"),
            )

    services = FormattingServices()
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    exit_code = main(
        ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
        dispatcher=runtime,
    )

    assert exit_code == 0
    assert services.raw_calls == 1
    assert services.files["ydb/docs/en/core/page.md"] == (
        b"* Parent translated\n"
        b"* `enable_strict_user_management` "
        b"\xe2\x80\x94 corrected item (i.e., only an administrator)\n"
    )
    assert services.events.count(("MODEL", ("files",))) == 1
    assert services.events.count(("MODEL", ("verdict", "findings"))) == 1
    assert services.events.count(("REPAIR", "markdown")) == 0


class ContentFilterServices(RuntimeServices):
    def __init__(self, filtered_responses: int) -> None:
        super().__init__()
        self.filtered_responses = filtered_responses
        self.raw_request_bodies: list[bytes] = []

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        schema_wrapper = request_schema(body)
        properties = None if schema_wrapper is None else schema_wrapper["schema"]["properties"]
        if properties is not None and all(key.startswith("segment_") for key in properties):
            self.raw_request_bodies.append(request.body)
            response = super().model(request)
            if len(self.raw_request_bodies) <= self.filtered_responses:
                document = json.loads(response.body)
                if "choices" in document:
                    document["choices"][0]["finish_reason"] = "content_filter"
                else:
                    document["result"]["alternatives"][0]["status"] = (
                        "ALTERNATIVE_STATUS_CONTENT_FILTER"
                    )
                return HttpResponse(
                    200,
                    json.dumps(document).encode(),
                    Decimal("0.01"),
                )
            return response
        return super().model(request)


def test_runtime_retries_one_content_filter_then_publishes_once() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = ContentFilterServices(filtered_responses=1)
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    exit_code = main(
        ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
        dispatcher=runtime,
    )

    attempts = [row for row in services.audit if "attempt_id" in row]
    translation_attempts = [row for row in attempts if row["role"] == "translate"]
    assert exit_code == 0
    assert services.files["ydb/docs/en/core/page.md"] == b"# Translated\n"
    assert len(services.raw_request_bodies) == 2
    assert services.raw_request_bodies[0] == services.raw_request_bodies[1]
    assert [row["status"] for row in translation_attempts] == ["failed", "succeeded"]
    assert [row["error"] for row in translation_attempts] == ["content_filter", None]
    assert [row["cost_rub"] for row in translation_attempts] == [
        Decimal("0.01"),
        Decimal("0.01"),
    ]
    assert (
        sum(method in {"POST", "PATCH"} and "/git/refs" in path for method, path in services.events)
        == 1
    )


def test_runtime_two_content_filters_fail_without_publication_or_checkpoint() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = ContentFilterServices(filtered_responses=4)
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    exit_code = main(
        ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
        dispatcher=runtime,
    )

    translation_attempts = [
        row for row in services.audit if "attempt_id" in row and row["role"] == "translate"
    ]
    assert exit_code == 1
    # Whole-file contract: identical request at most twice. No adaptive chunk split.
    assert len(services.raw_request_bodies) == 2
    assert services.raw_request_bodies[0] == services.raw_request_bodies[1]
    assert [row["status"] for row in translation_attempts] == ["failed", "failed"]
    assert [row["error"] for row in translation_attempts] == ["content_filter", "content_filter"]
    assert [row["cost_rub"] for row in translation_attempts] == [Decimal("0.01"), Decimal("0.01")]
    assert not any(method in {"POST", "PATCH"} for method, _path in services.events)
    assert all("continuation_id" not in row for row in services.audit)
    assert services.audit[-1]["status"] == "failed"


def test_runtime_content_filter_does_not_split_document_into_child_requests() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = ContentFilterServices(filtered_responses=2)
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    exit_code = main(
        ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
        dispatcher=runtime,
    )

    translation_attempts = [
        row for row in services.audit if "attempt_id" in row and row["role"] == "translate"
    ]
    assert exit_code == 1
    assert len(services.raw_request_bodies) == 2
    assert len({body for body in services.raw_request_bodies}) == 1
    assert [row["error"] for row in translation_attempts] == ["content_filter", "content_filter"]
    assert services.files["ydb/docs/en/core/page.md"] == b"# Old\n"
    assert not any(method in {"POST", "PATCH"} for method, _path in services.events)


def test_t017_f04_pure_rename_rejects_changed_whole_fence_before_commit() -> None:
    from ydbdoc_review_ng.application import TranslateWorkflowInput, WorkflowError
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.runtime import create_runtime

    class RenameServices(RuntimeServices):
        def github(self, method, path, payload):
            if path.endswith("/contents/ydb/docs/ru/core/old.md?ref=" + self.base):
                return {"type": "file", "encoding": "base64",
                        "content": base64.b64encode(b"```sql\nSELECT 1;\n```\n").decode()}
            if path.endswith("/pulls/42/files?per_page=100"):
                return [
                    {
                        "status": "renamed",
                        "filename": "ydb/docs/ru/core/page.md",
                        "previous_filename": "ydb/docs/ru/core/old.md",
                        "changes": 0,
                    }
                ]
            return super().github(method, path, payload)

    services = RenameServices()
    services.files["ydb/docs/ru/core/page.md"] = b"```sql\nSELECT 1;\n```\n"
    services.semantic_responses[0] = {
        "files": {
            "ydb/docs/en/core/page.md": "```sql\nDROP TABLE users;\n```\n",
        }
    }
    services.files["ydb/docs/en/core/old.md"] = b"```sql\nDROP TABLE users;\n```\n"
    del services.files["ydb/docs/en/core/page.md"]
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    with pytest.raises(WorkflowError):
        runtime.doc_translate(TranslateWorkflowInput(42, GitSha(services.source), Decimal(10)))

    assert not any(method in {"POST", "PATCH"} for method, _ in services.events)
    assert [row["role"] for row in services.audit if "attempt_id" in row] == ["direction", "critic"]
    assert services.audit[-1]["status"] == "failed"


@pytest.mark.parametrize("mismatch", ["delete", "rename", "toc", "redirect"])
def test_t017_f08_verify_rejects_missing_file_and_metadata_operations_before_critic(
    mismatch: str,
) -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    class OperationServices(RuntimeServices):
        def github(self, method, path, payload):
            if path.endswith("/pulls/42/files?per_page=100"):
                if mismatch == "delete":
                    return [{"status": "removed", "filename": "ydb/docs/ru/core/page.md"}]
                if mismatch == "toc":
                    return [{"status": "added", "filename": "ydb/docs/ru/core/page.md"}]
                return [
                    {
                        "status": "renamed",
                        "filename": "ydb/docs/ru/core/page.md",
                        "previous_filename": "ydb/docs/ru/core/old.md",
                        "changes": 0,
                    }
                ]
            return super().github(method, path, payload)

    services = OperationServices()
    services.branch_head = services.translated
    if mismatch == "delete":
        del services.files["ydb/docs/ru/core/page.md"]
    elif mismatch == "rename":
        services.files["ydb/docs/en/core/old.md"] = b"# Translated\n"
        del services.files["ydb/docs/en/core/page.md"]
    elif mismatch == "toc":
        services.files["ydb/docs/ru/core/toc.yaml"] = b"items:\n  - href: page.md\n"
        services.files["ydb/docs/en/core/toc.yaml"] = b"items:\n"
        services.files["ydb/docs/en/core/page.md"] = b"# Translated\n"
    else:
        services.files["ydb/docs/en/core/toc.yaml"] = b"items:\n  - href: page.md\n"
        services.files["ydb/docs/en/core/page.md"] = b"# Translated\n"
        services.files.pop("ydb/docs/en/core/old.md", None)
        services.files.pop("ydb/docs/en/redirects.yaml", None)
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    assert (
        main(
            [
                "verify",
                "--pr",
                "43",
                "--source-sha",
                services.source,
                "--target-sha",
                services.translated,
            ],
            dispatcher=runtime,
        )
        == 1
    )
    assert not any(method in {"POST", "PATCH", "MODEL"} for method, _ in services.events)
    assert services.audit[-1]["status"] == "failed"
    assert services.audit[-1]["error"] == "load_candidate_failed"


def test_t017_b3_verify_rename_requires_destination_toc_before_critic() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    class CompletedRenameServices(RuntimeServices):
        def __init__(self) -> None:
            super().__init__()
            self.ref_files = {
                self.source: {
                    "ydb/docs/ru/core/page.md": b"# Source\n",
                    "ydb/docs/en/core/old.md": b"# Translated\n",
                    "ydb/docs/ru/core/toc.yaml": b"items:\n  - href: page.md\n",
                    "ydb/docs/en/core/toc.yaml": b"items:\n  - href: old.md\n",
                },
                self.translated: {
                    "ydb/docs/en/core/page.md": b"# Translated\n",
                    "ydb/docs/en/core/toc.yaml": b"items:\n",
                    "ydb/docs/en/redirects.yaml": (
                        b'redirects:\n  - from: "core/old.md"\n    to: "core/page.md"\n'
                    ),
                },
            }

        def github(self, method, path, payload):
            if path.endswith("/pulls/42/files?per_page=100"):
                self.events.append((method, path))
                return [
                    {
                        "status": "renamed",
                        "filename": "ydb/docs/ru/core/page.md",
                        "previous_filename": "ydb/docs/ru/core/old.md",
                        "changes": 0,
                    }
                ]
            relative = path.removeprefix("/repos/ydb-platform/ydb")
            if relative.startswith("/contents/"):
                self.events.append((method, path))
                name, ref = relative[10:].split("?ref=", 1)
                content = self.ref_files.get(ref, {}).get(name)
                return (
                    None
                    if content is None
                    else {
                        "type": "file",
                        "encoding": "base64",
                        "content": base64.b64encode(content).decode(),
                    }
                )
            return super().github(method, path, payload)

    services = CompletedRenameServices()
    services.branch_head = services.translated
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    result = main(
        [
            "verify",
            "--pr",
            "43",
            "--source-sha",
            services.source,
            "--target-sha",
            services.translated,
        ],
        dispatcher=runtime,
    )

    external_mutations = [
        event for event in services.events if event[0] in {"POST", "PATCH", "MODEL"}
    ]
    assert (result, external_mutations) == (1, [])
    assert services.audit[-1]["status"] == "failed"
    assert services.audit[-1]["error"] == "load_candidate_failed"


class _T017R07Services(RuntimeServices):
    def __init__(self, operation: str, target_state: str) -> None:
        super().__init__()
        self.pr_exists = True
        self.operation = operation
        self.ref_files: dict[str, dict[str, bytes]] = {self.source: {}, self.translated: {}}
        if operation == "absent":
            if target_state == "restored":
                self.ref_files[self.translated]["ydb/docs/en/core/page.md"] = b"# Restored\n"
            return
        source_doc = b"# Source grpc://safe.example:2135 guide.md\n"
        translated_doc = b"# Translated grpc://safe.example:2135 guide.md\n"
        self.semantic_responses[0] = {
            "files": {
                "ydb/docs/en/core/page.md": "# Translated grpc://safe.example:2135 guide.md\n",
            }
        }
        self.ref_files[self.source] = {
            "ydb/docs/ru/core/page.md": source_doc,
            "ydb/docs/en/core/page.md": translated_doc,
            "ydb/docs/ru/core/toc.yaml": b"items:\n  - href: page.md\n",
            "ydb/docs/en/core/toc.yaml": b"items:\n  - href: page.md\n",
        }
        if target_state != "missing":
            self.ref_files[self.translated] = {
                "ydb/docs/en/core/page.md": (
                    translated_doc
                    if target_state == "correct"
                    else b"# Translated grpc://evil.example:2135 guide.md\n"
                ),
                "ydb/docs/en/core/toc.yaml": b"items:\n  - href: page.md\n",
                "ydb/docs/en/redirects.yaml": (
                    b'redirects:\n  - from: "core/old.md"\n    to: "core/page.md"\n'
                ),
            }

    def github(self, method, path, payload):
        if path.endswith("/pulls/42") and self.operation == "renamed":
            result = super().github(method, path, payload)
            return {**result, "changed_files": 2}
        if path.endswith("/pulls/42/files?per_page=100"):
            self.events.append((method, path))
            if self.operation == "absent":
                return [{"status": "removed", "filename": "ydb/docs/ru/core/page.md"}]
            return [
                {
                    "status": "renamed",
                    "filename": f"ydb/docs/{locale}/core/page.md",
                    "previous_filename": f"ydb/docs/{locale}/core/old.md",
                    "changes": 0,
                }
                for locale in ("ru", "en")
            ]
        relative = path.removeprefix("/repos/ydb-platform/ydb")
        if relative.startswith("/contents/"):
            self.events.append((method, path))
            name, ref = relative[10:].split("?ref=", 1)
            content = self.ref_files.get(ref, {}).get(name)
            return (
                None
                if content is None
                else {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                }
            )
        return super().github(method, path, payload)

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        schema_wrapper = request_schema(body)
        if schema_wrapper is None:
            return super().model(request)
        schema = schema_wrapper["schema"]
        if set(schema["properties"]) == {"page.md"}:
            self.events.append(("MODEL", ("page.md",)))
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
                                        "text": json.dumps({"page.md": "ru_to_en"}),
                                    },
                                }
                            ],
                            "usage": {"inputTextTokens": "10", "completionTokens": "5"},
                        }
                    }
                ).encode(),
                Decimal("0.01"),
            )
        return super().model(request)


def _run_t017_r07(services: _T017R07Services) -> int:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services.branch_head = services.translated
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    return main(
        [
            "verify",
            "--pr",
            "43",
            "--source-sha",
            services.source,
            "--target-sha",
            services.translated,
        ],
        dispatcher=runtime,
    )


@pytest.mark.parametrize(
    ("target_state", "expected_exit"),
    [("restored", 1), ("absent", 0)],
)
def test_t017_r07_target_absent_noop_checks_pinned_target(
    target_state: str, expected_exit: int
) -> None:
    services = _T017R07Services("absent", target_state)

    assert _run_t017_r07(services) == expected_exit
    assert any(
        path.endswith("/contents/ydb/docs/en/core/page.md?ref=" + services.translated)
        for method, path in services.events
        if method == "GET"
    )
    assert ("MODEL", ("verdict", "findings")) not in services.events
    if expected_exit:
        assert services.audit[-1]["error"] == "load_candidate_failed"


@pytest.mark.parametrize(
    ("target_state", "expected_exit", "expects_critic"),
    [("missing", 1, False), ("protected-changed", 1, False), ("correct", 0, True)],
)
def test_t017_r07_already_renamed_noop_checks_entire_pinned_target(
    target_state: str, expected_exit: int, expects_critic: bool
) -> None:
    services = _T017R07Services("renamed", target_state)

    assert _run_t017_r07(services) == expected_exit
    assert (("MODEL", ("files",)) in services.events) is expects_critic
    if expected_exit:
        assert services.audit[-1]["error"] in {"load_candidate_failed", "validate_failed"}


def test_verify_refuses_unbound_source_sha_before_models_or_mutations():
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = RuntimeServices()
    services.branch_head = services.translated
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "m",
            "YDBDOC_ALLOWED_ACTORS": "m",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    assert (
        main(
            ["verify", "--pr", "43", "--source-sha", "f" * 40, "--target-sha", services.translated],
            dispatcher=runtime,
        )
        == 1
    )
    assert not any(method in {"POST", "PATCH", "MODEL"} for method, _ in services.events)
    assert services.audit[-1]["status"] == "failed"
    assert "authorize" in services.audit[-1]["error"]


def test_identical_runtime_candidate_cannot_create_pr_or_comment():
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = RuntimeServices()
    services.files["ydb/docs/en/core/page.md"] = b"# Translated\n"
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "m",
            "YDBDOC_ALLOWED_ACTORS": "m",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    assert (
        main(
            ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
            dispatcher=runtime,
        )
        == 0
    )
    assert not services.pr_exists
    assert not services.comments
    assert not any(method in {"POST", "PATCH"} for method, _ in services.events)


def test_production_factory_is_shipped() -> None:
    assert importlib.util.find_spec("ydbdoc_review_ng.runtime") is not None


def test_runtime_denied_actor_is_audited_without_github_or_model_io() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    class Audit:
        def __init__(self):
            self.rows = []

        def execute(self, statement, parameters):
            self.rows.append(dict(parameters))
            return []

    def forbidden(*args):
        pytest.fail("unauthorized external I/O")

    audit = Audit()
    runtime = create_runtime(
        environment={"GITHUB_ACTOR": "outsider", "YDBDOC_ALLOWED_ACTORS": "maintainer"},
        ydb_executor=audit,
        github_transport=forbidden,
        model_transport=forbidden,
    )
    assert (
        main(
            ["translate", "--pr", "42", "--source-sha", "a" * 40, "--budget-rub", "10"],
            dispatcher=runtime,
        )
        == 1
    )
    assert [row.get("status") for row in audit.rows] == ["started", "failed"]


def test_t017_r02_rerun_authorizes_triggering_actor_before_external_io() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = RuntimeServices()
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "GITHUB_TRIGGERING_ACTOR": "not-allowed",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    assert (
        main(
            ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
            dispatcher=runtime,
        )
        == 1
    )
    assert services.events == []
    assert [row.get("status") for row in services.audit] == ["started", "failed"]
    assert services.audit[-1]["error"] == "authorize_failed"


def test_t017_n01_real_verify_rejects_sentence_final_filename_change() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = RuntimeServices()
    services.branch_head = services.translated
    services.pr_exists = True
    services.files["ydb/docs/ru/core/page.md"] = b"Open guide.md.\n"
    services.files["ydb/docs/en/core/page.md"] = b"Open other.md.\n"
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    result = main(
        [
            "verify",
            "--pr",
            "43",
            "--source-sha",
            services.source,
            "--target-sha",
            services.translated,
        ],
        dispatcher=runtime,
    )

    assert result == 1
    assert ("MODEL", ("verdict", "findings")) not in services.events
    assert services.comments == []
    assert services.audit[-1]["status"] == "failed"
    assert services.audit[-1]["error"] == "validate_failed"


def _assert_t017_q01_real_verify_rejects_literal_change(source: bytes) -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = RuntimeServices()
    services.branch_head = services.translated
    services.pr_exists = True
    services.files["ydb/docs/ru/core/page.md"] = source
    services.files["ydb/docs/en/core/page.md"] = source.replace(b"not a comment", b"CHANGED STRING")
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    result = main(
        [
            "verify",
            "--pr",
            "43",
            "--source-sha",
            services.source,
            "--target-sha",
            services.translated,
        ],
        dispatcher=runtime,
    )

    assert result == 1
    assert ("MODEL", ("verdict", "findings")) not in services.events
    assert services.comments == []
    assert services.audit[-1]["status"] == "failed"
    assert services.audit[-1]["error"] == "validate_failed"


def test_t017_q01_real_verify_rejects_sequence_block_scalar_change() -> None:
    _assert_t017_q01_real_verify_rejects_literal_change(
        b"```yaml\nsteps:\n  - run: |\n      # not a comment\n"
        b"      Last line\n# Real comment\n```\n"
    )


def test_t017_q01_real_verify_rejects_quoted_key_block_scalar_change() -> None:
    _assert_t017_q01_real_verify_rejects_literal_change(
        b'```yaml\n"value": |\n  # not a comment\n  Last line\n# Real comment\n```\n'
    )


class _T017N04Services(RuntimeServices):
    def __init__(self):
        super().__init__()
        self.semantic_responses[0] = {
            "files": {
                "ydb/docs/en/core/page.md": _t017_n04_documents()[1].decode(),
            }
        }

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        schema_wrapper = request_schema(body)
        if schema_wrapper is None:
            self.events.append(("MODEL", ("raw_markdown",)))
            text = _t017_n04_documents()[1].decode()
            return HttpResponse(
                200,
                json.dumps(
                    {
                        "result": {
                            "alternatives": [
                                {
                                    "status": "ALTERNATIVE_STATUS_FINAL",
                                    "message": {"role": "assistant", "text": text},
                                }
                            ],
                            "usage": {"inputTextTokens": "10", "completionTokens": "5"},
                        }
                    }
                ).encode(),
                Decimal("0.01"),
            )
        schema = schema_wrapper["schema"]
        properties = tuple(schema["properties"])
        if "translation_required" in properties:
            return super().model(request)
        self.events.append(("MODEL", properties))
        if properties == ("page.md",):
            values = {"page.md": "ru_to_en"}
        elif properties and all(key.startswith("segment_") for key in properties):
            values = {
                key: value.replace('An \\"escaped\\" title', 'A \\"quoted\\" title')
                for key, value in translation_segments(request_prompt(body)).items()
            }
        elif set(properties) in ({"files"}, {"verdict", "findings"}):
            assert self.semantic_responses
            values = self.semantic_responses.pop(0)
        else:
            values = {field_id: 'A "quoted" title' for field_id in properties}
        return HttpResponse(
            200,
            json.dumps(
                {
                    "result": {
                        "alternatives": [
                            {
                                "status": "ALTERNATIVE_STATUS_FINAL",
                                "message": {"role": "assistant", "text": json.dumps(values)},
                            }
                        ],
                        "usage": {"inputTextTokens": "10", "completionTokens": "5"},
                    }
                }
            ).encode(),
            Decimal("0.01"),
        )


def _t017_n04_documents() -> tuple[bytes, bytes]:
    from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
    from ydbdoc_review_ng.parser.markdown import build_markdown_plan
    from ydbdoc_review_ng.translation import assemble_candidate, build_translation_request

    source = b'---\ntitle: "An \\"escaped\\" title"\n---\n'
    snapshot = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
    plan = build_markdown_plan(snapshot, RepoPath("ydb/docs/ru/core/page.md"), source)
    request = build_translation_request(source, plan)
    target = assemble_candidate(
        source,
        plan,
        request,
        {field.field_id: 'A "quoted" title' for field in request.fields},
    )
    return source, target


def test_t017_n04_real_translate_reviews_escaped_quoted_frontmatter() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    source, target = _t017_n04_documents()
    services = _T017N04Services()
    services.files["ydb/docs/ru/core/page.md"] = source
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    result = main(
        ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
        dispatcher=runtime,
    )

    assert result == 0
    assert services.files["ydb/docs/en/core/page.md"] == target
    assert ("MODEL", ("files",)) in services.events
    assert services.comments[0]["body"].startswith("🟢 GREEN\n")


def test_t017_n04_real_verify_reviews_escaped_quoted_frontmatter() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    source, target = _t017_n04_documents()
    services = _T017N04Services()
    services.branch_head = services.translated
    services.pr_exists = True
    services.files["ydb/docs/ru/core/page.md"] = source
    services.files["ydb/docs/en/core/page.md"] = target
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    result = main(
        [
            "verify",
            "--pr",
            "43",
            "--source-sha",
            services.source,
            "--target-sha",
            services.translated,
        ],
        dispatcher=runtime,
    )

    assert result == 0
    assert [event for event in services.events if event[0] == "MODEL"] == [
        ("MODEL", ("files",)),
        ("MODEL", ("verdict", "findings")),
    ]
    assert services.comments[0]["body"].startswith("🟢 GREEN\n")


def test_t017_n05_truncated_http_response_is_audited_once_with_unknown_cost() -> None:
    import http.client
    import io
    from unittest.mock import patch

    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.models import UrllibTransport
    from ydbdoc_review_ng.runtime import create_runtime

    partial = b'{"result":'

    class MemorySocket:
        def makefile(self, _mode):
            return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + partial)

    def truncated_response(*_args, **_kwargs):
        response = http.client.HTTPResponse(MemorySocket())
        response.begin()
        return response

    services = RuntimeServices()
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=UrllibTransport(),
    )

    with patch("urllib.request.urlopen", side_effect=truncated_response) as boundary:
        result = main(
            [
                "translate",
                "--pr",
                "42",
                "--source-sha",
                services.source,
                "--budget-rub",
                "10",
            ],
            dispatcher=runtime,
        )

    attempts = [row for row in services.audit if "attempt_id" in row]
    assert result == 1
    assert boundary.call_count == 2
    assert len(attempts) == 2
    assert all(attempt["status"] == "failed" for attempt in attempts)
    assert all(attempt["error"] == "transport" for attempt in attempts)
    assert all(attempt["response"] == partial for attempt in attempts)
    assert all(attempt["cost_rub"] is None for attempt in attempts)
    assert services.audit[-1]["status"] == "failed"
    assert services.audit[-1]["error"] == "prepare_failed"
    assert len(services.source_comments) == 1
    assert not any(method in {"POST", "PATCH"} and "/git/" in path
                   for method, path in services.events)


def test_t017_q02_truncated_http_error_body_is_audited_once_with_unknown_cost() -> None:
    import http.client
    import io
    import urllib.error
    from unittest.mock import patch

    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.models import UrllibTransport
    from ydbdoc_review_ng.runtime import create_runtime

    partial = b'{"result":'

    class MemorySocket:
        def makefile(self, _mode):
            return io.BytesIO(
                b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 1000\r\n\r\n" + partial
            )

    def truncated_error(*_args, **_kwargs):
        response = http.client.HTTPResponse(MemorySocket())
        response.begin()
        raise urllib.error.HTTPError(
            "https://offline.invalid",
            response.status,
            response.reason,
            response.headers,
            response,
        )

    services = RuntimeServices()
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=UrllibTransport(),
    )

    with patch("urllib.request.urlopen", side_effect=truncated_error) as boundary:
        result = main(
            [
                "translate",
                "--pr",
                "42",
                "--source-sha",
                services.source,
                "--budget-rub",
                "10",
            ],
            dispatcher=runtime,
        )

    attempts = [row for row in services.audit if "attempt_id" in row]
    assert result == 1
    assert boundary.call_count == 2
    assert len(attempts) == 2
    assert all(attempt["status"] == "failed" for attempt in attempts)
    assert all(attempt["error"] == "transport" for attempt in attempts)
    assert all(attempt["response"] == partial for attempt in attempts)
    assert all(attempt["cost_rub"] is None for attempt in attempts)
    assert services.audit[-1]["status"] == "failed"
    assert services.audit[-1]["error"] == "prepare_failed"
    assert len(services.source_comments) == 1
    assert not any(method in {"POST", "PATCH"} and "/git/" in path
                   for method, path in services.events)


def test_github_backend_commits_exact_bytes_and_rejects_changed_head() -> None:
    from ydbdoc_review_ng.domain import GitSha, RepoPath
    from ydbdoc_review_ng.publication import FileChange, PublicationContext, PublicationPlan
    from ydbdoc_review_ng.runtime_github import GitHubBackend, RuntimeBoundaryError

    calls = []

    def transport(method, path, payload):
        calls.append((method, path, payload))
        if path.endswith("/git/commits/" + "a" * 40):
            return {"tree": {"sha": "b" * 40}}
        if path.endswith("/git/blobs"):
            return {"sha": "c" * 40}
        if path.endswith("/git/trees"):
            return {"sha": "d" * 40}
        if path.endswith("/git/commits"):
            return {"sha": "e" * 40}
        if "/git/ref/" in path:
            return {"object": {"sha": "f" * 40}}
        raise AssertionError(path)

    backend = GitHubBackend(transport)
    context = PublicationContext(
        "ydb-platform/ydb", "translation/pr-42", "main", "main", GitSha("a" * 40)
    )
    plan = PublicationPlan((FileChange(RepoPath("en/page.md"), b"old", b"new"),), ())
    sha = backend.commit(context, plan)
    assert sha.value == "e" * 40
    assert calls[1][2] == {"encoding": "base64", "content": "bmV3"}
    assert calls[-1][2]["parents"] == ["a" * 40]
    with pytest.raises(RuntimeBoundaryError, match="head_changed"):
        backend.push(context, sha)
    assert not any(method == "PATCH" for method, _, _ in calls)


def test_sdk_executor_supplies_explicit_nullable_and_decimal_types() -> None:
    from ydbdoc_review_ng.runtime_ydb import parameter_types

    assert parameter_types({"target_sha": None, "cost_rub": Decimal(0), "pr_number": 42}) == {
        "target_sha": "Utf8?",
        "cost_rub": "Decimal(22,9)?",
        "pr_number": "Uint64",
    }


def test_metadata_producer_uses_pinned_source_toc_for_add_and_target_preimage_for_rename():
    from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
    from ydbdoc_review_ng.runtime_metadata import MetadataProducer

    source = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
    target = SnapshotRef(source.repository, GitSha("b" * 40))
    toc = {
        ("a" * 40, "ydb/docs/ru/core/toc.yaml"): b"items:\n  - name: New\n    href: new.md\n",
        ("b" * 40, "ydb/docs/en/core/toc.yaml"): b"items:\n  - name: Old\n    href: old.md\n",
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            assert snapshot in {source, target}
            return toc.get((snapshot.commit_sha.value, path.value))

    producer = MetadataProducer(Reader(), source, target, (RepoPath("ydb/docs/ru/core/toc.yaml"),))
    add = producer.changes(
        RepoPath("ydb/docs/ru/core/new.md"), RepoPath("ydb/docs/en/core/new.md"), new=True
    )
    assert len(add) == 1
    assert add[0].path.value == "ydb/docs/en/core/toc.yaml"
    assert b"href: new.md" in add[0].after
    assert b"href: old.md" in add[0].after
    assert (
        producer.changes(
            RepoPath("ydb/docs/ru/core/unreachable.md"),
            RepoPath("ydb/docs/en/core/unreachable.md"),
            new=True,
        )
        == ()
    )
    rename = producer.changes(
        RepoPath("ydb/docs/ru/core/new.md"),
        RepoPath("ydb/docs/en/core/new.md"),
        old=RepoPath("ydb/docs/en/core/old.md"),
    )
    assert b"href: old.md" not in rename[0].after
    assert b"href: new.md" in rename[0].after
    assert rename[1].after == b'redirects:\n  - from: "core/old.md"\n    to: "core/new.md"\n'


def test_metadata_producer_rejects_redirect_chains_before_publication():
    from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
    from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError
    from ydbdoc_review_ng.runtime_metadata import MetadataProducer

    snapshot = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))

    class Reader:
        def read_bytes(self, snapshot, path):
            if path.value.endswith("redirects.yaml"):
                return b'redirects:\n  - from: "core/new.md"\n    to: "core/final.md"\n'
            return b"items:\n  - name: Old\n    href: old.md\n"

    producer = MetadataProducer(Reader(), snapshot, snapshot, ())
    with pytest.raises(RuntimeBoundaryError, match="redirect_chain"):
        producer.changes(
            RepoPath("ydb/docs/ru/core/new.md"),
            RepoPath("ydb/docs/en/core/new.md"),
            old=RepoPath("ydb/docs/en/core/old.md"),
        )


def test_two_new_pages_accumulate_into_one_toc_candidate():
    from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
    from ydbdoc_review_ng.runtime_metadata import MetadataProducer

    source = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
    target = SnapshotRef(source.repository, GitSha("b" * 40))

    class Reader:
        def read_bytes(self, snapshot, path):
            if snapshot == source:
                return b"items:\n  - name: One\n    href: one.md\n  - name: Two\n    href: two.md\n"
            return b"items:\n"

    pending = {}
    producer = MetadataProducer(Reader(), source, target, (), pending=pending)
    for name in ("one", "two"):
        for change in producer.changes(
            RepoPath(f"ydb/docs/ru/core/{name}.md"),
            RepoPath(f"ydb/docs/en/core/{name}.md"),
            new=True,
        ):
            pending[change.path.value] = change.after
    assert pending["ydb/docs/en/core/toc.yaml"].count(b"href:") == 2


def test_pr50839_full_runtime_plan_publishes_exact_complete_toc() -> None:
    from ydbdoc_review_ng.application import TranslateWorkflowInput
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.runtime import create_runtime

    directory = "ydb/docs/{}/core/maintenance/manual/"
    fixture_root = Path(__file__).parents[1] / "fixtures"
    ru_toc_before = (fixture_root / "pr50839_ru_toc_i_before.yaml").read_bytes()
    ru_toc = (fixture_root / "pr50839_ru_toc_i.yaml").read_bytes()
    en_before = (fixture_root / "pr50839_en_toc_i_before.yaml").read_bytes()
    en_expected = (fixture_root / "pr50839_en_toc_i_expected.yaml").read_bytes()

    class Services(RuntimeServices):
        merge_commit = "9" * 40

        def __init__(self) -> None:
            super().__init__()
            self.blobs: dict[str, bytes] = {}
            self.published: dict[str, bytes | None] = {}
            self.ru_toc_before = ru_toc_before
            for name, ru_heading, en_heading in (
                ("blobdepot.md", "BlobDepot", "BlobDepot"),
                (
                    "blobdepot_decommit.md",
                    "\xd0\x94\xd0\xb5\xd0\xba\xd0\xbe\xd0\xbc\xd0\xb8\xd1\x81\xd1\x81\xd0\xb8\xd1\x8f BlobDepot",
                    "Group Decommissioning",
                ),
                (
                    "index.md",
                    "\xd0\x9e\xd0\xb1\xd1\x81\xd0\xbb\xd1\x83\xd0\xb6\xd0\xb8\xd0\xb2\xd0\xb0\xd0\xbd\xd0\xb8\xd0\xb5",
                    "Maintenance",
                ),
            ):
                self.files[directory.format("ru") + name] = f"# {ru_heading}\n".encode()
                self.files[directory.format("en") + name] = f"# {en_heading}\n".encode()
            self.files[directory.format("ru") + "toc_i.yaml"] = ru_toc
            self.files[directory.format("en") + "toc_i.yaml"] = en_before
            self.semantic_responses[0] = {
                "files": {
                    directory.format("en") + "blobdepot.md": "# Translated\n",
                    directory.format("en") + "blobdepot_decommit.md": "# Translated\n",
                    directory.format("en") + "index.md": "# Translated\n",
                    directory.format("en") + "toc_i.yaml": en_expected.decode(),
                }
            }

        def github(self, method, path, payload):
            normalized = path.removeprefix("/repos/ydb-platform/ydb")
            source_toc = directory.format("ru") + "toc_i.yaml"
            if normalized == f"/contents/{source_toc}?ref={self.base}":
                return {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(self.ru_toc_before).decode(),
                }
            if normalized == f"/contents/{source_toc}?ref={self.merge_commit}":
                return {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(ru_toc).decode(),
                }
            if normalized == "/pulls/42":
                result = super().github(method, path, payload)
                result.update(
                    merged=True,
                    merge_commit_sha=self.merge_commit,
                    changed_files=4,
                )
                return result
            if normalized == "/pulls/42/files?per_page=100":
                return [
                    {"status": "modified", "filename": directory.format("ru") + name}
                    for name in (
                        "blobdepot.md",
                        "blobdepot_decommit.md",
                        "index.md",
                        "toc_i.yaml",
                    )
                ]
            if normalized == "/git/blobs":
                content = base64.b64decode(payload["content"])
                sha = f"{len(self.blobs) + 1:040x}"
                self.blobs[sha] = content
                return {"sha": sha}
            if normalized == "/git/trees":
                for row in payload["tree"]:
                    self.published[row["path"]] = (
                        None if row["sha"] is None else self.blobs[row["sha"]]
                    )
                return {"sha": "d" * 40}
            return super().github(method, path, payload)

    services = Services()
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "m",
            "YDBDOC_ALLOWED_ACTORS": "m",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )

    result = runtime.doc_translate(TranslateWorkflowInput(42, GitSha(services.source), Decimal(10)))

    assert result.final_commit_sha == GitSha(services.translated)
    assert services.published[directory.format("en") + "toc_i.yaml"] == en_expected
    assert b"Dynamic cluster configuration" in en_expected
    assert b"BlobDepot decommit" not in en_expected


@pytest.mark.parametrize(
    "toc_label", ["Group Decommissioning", "Corrected BlobDepot decommissioning"]
)
def test_verify_replays_pinned_toc_plan_instead_of_translated_h1(toc_label) -> None:
    from ydbdoc_review_ng.application import VerifyWorkflowInput
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.runtime import RuntimeSource
    from ydbdoc_review_ng.runtime_content import RuntimeContent, unpack
    from ydbdoc_review_ng.runtime_github import GitHubBackend

    directory = "ydb/docs/{}/core/maintenance/manual/"

    class Services(RuntimeServices):
        def __init__(self) -> None:
            super().__init__()
            self.branch_head = self.translated
            self.ref_files = {
                self.source: {
                    directory.format("ru") + "blobdepot_decommit.md": (
                        b"# Decommission source\n\nCurrent source.\n\nUnchanged paragraph.\n"
                    ),
                    directory.format("ru") + "toc_i.yaml": (
                        "items:\n  - name: Декомиссия BlobDepot\n    href: blobdepot_decommit.md\n"
                    ).encode(),
                },
                self.base: {
                    directory.format("ru") + "toc_i.yaml": b"items:\n",
                    directory.format("en") + "blobdepot_decommit.md": (
                        b"# Group Decommissioning\n"
                    ),
                    directory.format("en") + "toc_i.yaml": (
                        b"items:\n  - name: BlobDepot decommit\n    href: blobdepot_decommit.md\n"
                    ),
                },
                self.translated: {
                    # A translated heading is prose and may use different
                    # capitalization than the already frozen navigation label.
                    directory.format("en") + "blobdepot_decommit.md": (
                        b"# Group decommissioning\n\nCurrent translation.\n\nUnchanged paragraph.\n"
                    ),
                    directory.format("en") + "toc_i.yaml": (
                        f'items:\n  - name: "{toc_label}"\n    href: blobdepot_decommit.md\n'
                    ).encode(),
                },
            }
            self.files = dict(self.ref_files[self.translated])

        def github(self, method, path, payload):
            if path.endswith("/pulls/42"):
                result = super().github(method, path, payload)
                return {**result, "changed_files": 2}
            if path.endswith("/pulls/42/files?per_page=100"):
                return [
                    {"status": "modified", "filename": directory.format("ru") + name}
                    for name in ("blobdepot_decommit.md", "toc_i.yaml")
                ]
            relative = path.removeprefix("/repos/ydb-platform/ydb")
            if relative.startswith("/contents/"):
                name, ref = relative[10:].split("?ref=", 1)
                content = self.ref_files.get(ref, {}).get(name)
                return (
                    None
                    if content is None
                    else {
                        "type": "file",
                        "encoding": "base64",
                        "content": base64.b64encode(content).decode(),
                    }
                )
            return super().github(method, path, payload)

    class Models:
        def invoke(self, request):
            raise AssertionError("candidate loading must not call models")

    services = Services()
    source = RuntimeSource(
        {"GITHUB_ACTOR": "maintainer", "YDBDOC_ALLOWED_ACTORS": "maintainer"},
        GitHubBackend(services.github),
    )
    request = VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))

    authorized = source.authorize_verify(request)
    snapshot = source.snapshot_verify(authorized)
    content = RuntimeContent(source, Models(), {})
    candidate = content.load_verification_candidate(snapshot)
    files = unpack(candidate.content)

    assert f'"{toc_label}"'.encode() in files[directory.format("en") + "toc_i.yaml"]
    assert files[directory.format("en") + "blobdepot_decommit.md"] == (
        b"# Group decommissioning\n\nCurrent translation.\n\nUnchanged paragraph.\n"
    )
    source_files, translated_files, glossary_files = content._pr_review_inputs(candidate)
    assert source_files == {
        directory.format("ru") + "blobdepot_decommit.md": (
            b"# Decommission source\n\nCurrent source.\n\nUnchanged paragraph.\n"
        ),
        directory.format("ru") + "toc_i.yaml": (
            "items:\n  - name: Декомиссия BlobDepot\n    href: blobdepot_decommit.md\n"
        ).encode(),
    }
    assert translated_files == {
        directory.format("en") + "blobdepot_decommit.md": (
            b"# Group decommissioning\n\nCurrent translation.\n\nUnchanged paragraph.\n"
        ),
        directory.format("en") + "toc_i.yaml": (
            f'items:\n  - name: "{toc_label}"\n    href: blobdepot_decommit.md\n'
        ).encode(),
    }
    assert glossary_files == {}
    assert all(method == "GET" for method, _ in services.events)


@pytest.mark.parametrize("operation", ["added", "removed", "renamed"])
def test_runtime_canonical_file_operations_have_shipped_producer(operation):
    from ydbdoc_review_ng.application import TranslateWorkflowInput
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.runtime import create_runtime

    class Services(RuntimeServices):
        def github(self, method, path, payload):
            before_name = "old.md" if operation == "renamed" else "page.md"
            if path.endswith("/contents/ydb/docs/ru/core/" + before_name + "?ref=" + self.base):
                return {"type": "file", "encoding": "base64",
                        "content": base64.b64encode(b"# Source preimage\n").decode()}
            if path.endswith("/pulls/42/files?per_page=100"):
                return [
                    {
                        "status": operation,
                        "filename": "ydb/docs/ru/core/page.md",
                        "previous_filename": "ydb/docs/ru/core/old.md",
                        "changes": 0,
                    }
                ]
            return super().github(method, path, payload)

    services = Services()
    if operation == "added":
        del services.files["ydb/docs/en/core/page.md"]
        services.files["ydb/docs/ru/core/toc.yaml"] = b"items:\n  - name: Page\n    href: page.md\n"
        services.files["ydb/docs/en/core/toc.yaml"] = b"items:\n"
    elif operation == "removed":
        del services.files["ydb/docs/ru/core/page.md"]
    else:
        services.files["ydb/docs/en/core/old.md"] = b"# Old\n"
        del services.files["ydb/docs/en/core/page.md"]
        services.files["ydb/docs/en/core/toc.yaml"] = b"items:\n  - name: Old\n    href: old.md\n"
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "m",
            "YDBDOC_ALLOWED_ACTORS": "m",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    result = runtime.doc_translate(TranslateWorkflowInput(42, GitSha(services.source), Decimal(10)))
    assert result.final_commit_sha == GitSha(services.translated)
    model_calls = [event for event in services.events if event[0] == "MODEL"]
    assert len(model_calls) == {"added": 3, "removed": 0, "renamed": 2}[operation]
    assert len(services.comments) == 1


def test_exhausted_runtime_budget_stops_before_direction_or_translation():
    from ydbdoc_review_ng.application import TranslateWorkflowInput
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.persistence import DailyBudgetExceeded
    from ydbdoc_review_ng.runtime import create_runtime

    services = RuntimeServices()
    runtime = create_runtime(
        environment={"GITHUB_ACTOR": "m", "YDBDOC_ALLOWED_ACTORS": "m"},
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    with pytest.raises(DailyBudgetExceeded):
        runtime.doc_translate(TranslateWorkflowInput(42, GitSha(services.source), Decimal(0)))
    assert not any(method in {"POST", "PATCH", "MODEL"} for method, _ in services.events)
    assert services.audit[-1]["status"] == "failed"


def test_ydb_sdk_executor_uses_real_typed_values_with_injected_connection(monkeypatch):
    import sys
    from types import SimpleNamespace

    from ydbdoc_review_ng.runtime_ydb import SDKExecutor

    calls = []
    pool = SimpleNamespace(
        execute_with_retries=lambda query, params: (
            calls.append((query, params))
            or [SimpleNamespace(rows=[{"total_cost_rub": Decimal("1.25")}])]
        )
    )
    driver = SimpleNamespace(wait=lambda **kwargs: None)
    sdk = SimpleNamespace(
        Driver=lambda **kwargs: driver,
        QuerySessionPool=lambda supplied: pool,
        AccessTokenCredentials=lambda token: object(),
        PrimitiveType=SimpleNamespace(Utf8="Utf8", Uint64="Uint64"),
        DecimalType=lambda p, s: f"Decimal({p},{s})",
        OptionalType=lambda value: value + "?",
        TypedValue=lambda value, value_type: (value, value_type),
    )
    monkeypatch.setitem(sys.modules, "ydb", sdk)
    executor = SDKExecutor("grpcs://db.example:2135", "/database", "secret")
    rows = executor.execute("SELECT $cost_rub", {"cost_rub": Decimal(0), "target_sha": None})
    assert rows == [{"total_cost_rub": Decimal("1.25")}]
    assert calls[0][1] == {
        "$cost_rub": (Decimal(0), "Decimal(22,9)?"),
        "$target_sha": (None, "Utf8?"),
    }
    assert "DECLARE $cost_rub AS Decimal(22,9)?;" in calls[0][0]
    assert "secret" not in calls[0][0]


def test_ydb_sdk_executor_accepts_deployed_inline_service_account_key(monkeypatch):
    import sys
    from types import SimpleNamespace

    from ydbdoc_review_ng.runtime_ydb import SDKExecutor

    observed = []
    credentials = object()
    pool = SimpleNamespace(execute_with_retries=lambda query, params: [])
    driver = SimpleNamespace(wait=lambda **kwargs: None)
    sdk = SimpleNamespace(
        Driver=lambda **kwargs: observed.append(kwargs) or driver,
        QuerySessionPool=lambda supplied: pool,
        AccessTokenCredentials=lambda token: pytest.fail("access token path must not be used"),
        iam=SimpleNamespace(
            ServiceAccountCredentials=SimpleNamespace(
                from_content=lambda content: observed.append(content) or credentials
            )
        ),
        PrimitiveType=SimpleNamespace(Utf8="Utf8"),
        TypedValue=lambda value, value_type: (value, value_type),
    )
    monkeypatch.setitem(sys.modules, "ydb", sdk)

    executor = SDKExecutor("grpcs://db.example:2135", "/database", "", '{"id":"sa"}')
    assert executor.execute("SELECT 1", {}) == []
    assert observed == [
        '{"id":"sa"}',
        {
            "endpoint": "grpcs://db.example:2135",
            "database": "/database",
            "credentials": credentials,
        },
    ]


def test_runtime_uses_deployed_ydb_service_account_and_defaults(monkeypatch):
    import ydbdoc_review_ng.runtime as runtime_module
    from ydbdoc_review_ng.runtime import create_runtime
    from ydbdoc_review_ng.runtime_ydb import DEFAULT_YDB_DATABASE, DEFAULT_YDB_ENDPOINT

    captured = []
    shutdowns = []

    class CapturingExecutor:
        def __init__(self, endpoint, database, token, service_account_key):
            captured.append((endpoint, database, token, service_account_key))

        def execute(self, statement, parameters):
            return []

        def close(self):
            shutdowns.append("executor")

    monkeypatch.setattr(runtime_module, "SDKExecutor", CapturingExecutor)
    runtime = create_runtime(environment={"YDB_SA_KEY": '{"id":"sa"}'})

    assert captured == [(DEFAULT_YDB_ENDPOINT, DEFAULT_YDB_DATABASE, "", '{"id":"sa"}')]
    runtime.shutdown()
    runtime.shutdown()
    assert shutdowns == ["executor"]


def test_critic_edit_is_validated_then_published_once_before_pr():
    from ydbdoc_review_ng.application import TranslateWorkflowInput
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.models import HttpResponse
    from ydbdoc_review_ng.runtime import create_runtime

    class Services(RuntimeServices):
        def __init__(self):
            super().__init__()
            self.semantic_responses[0] = {
                "files": {"ydb/docs/en/core/page.md": "# Corrected\n"},
            }

        def model(self, request):
            from _runtime_services import request_schema

            schema = request_schema(json.loads(request.body))
            response = super().model(request)
            if schema is not None and set(schema["schema"]["properties"]) == {"files"}:
                self.events[-1] = ("CRITIC", "first")
                return HttpResponse(response.status_code, response.body, Decimal("0.02"))
            return response

    services = Services()
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "m",
            "YDBDOC_ALLOWED_ACTORS": "m",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    result = runtime.doc_translate(TranslateWorkflowInput(42, GitSha(services.source), Decimal(10)))
    assert result.repair_applied
    assert services.files["ydb/docs/en/core/page.md"] == b"# Corrected\n"
    significant = [
        method if method in {"CRITIC", "REPAIR", "MODEL"} else path.rsplit("/", 1)[-1]
        for method, path in services.events
        if method in {"CRITIC", "REPAIR", "MODEL"}
        or (method == "POST" and path.endswith(("/git/commits", "/pulls")))
    ]
    assert significant == ["MODEL", "CRITIC", "MODEL", "commits", "pulls"]
    assert "Стоимость запуска: 0.05 RUB" in services.comments[0]["body"]


def test_runtime_never_reports_green_after_branch_moves_during_critic():
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    class Services(RuntimeServices):
        def model(self, request):
            response = super().model(request)
            schema_wrapper = request_schema(json.loads(request.body))
            if schema_wrapper and "findings" in schema_wrapper["schema"]["properties"]:
                self.branch_head = "f" * 40
            return response

    services = Services()
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "m",
            "YDBDOC_ALLOWED_ACTORS": "m",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    assert (
        main(
            ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
            dispatcher=runtime,
        )
        == 1
    )
    assert not services.pr_exists
    assert not services.comments


def test_broken_build_does_not_block_translation_publication(monkeypatch, tmp_path):
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.diplodoc import DiplodocBuildError, DiplodocBuildValidator
    from ydbdoc_review_ng.runtime import create_runtime

    services = RuntimeServices()
    calls = []

    def reject_build(self, *args):
        calls.append(self.docs_root)
        raise DiplodocBuildError(("ERR ru/changelog-server.md: unreachable link",))

    monkeypatch.setattr(DiplodocBuildValidator, "validate_baseline", reject_build)
    monkeypatch.setattr(DiplodocBuildValidator, "__call__", reject_build)
    runtime = create_runtime(
        environment={
            "GITHUB_ACTOR": "maintainer",
            "YDBDOC_ALLOWED_ACTORS": "maintainer",
            "YANDEX_API_KEY": "secret",
            "YANDEX_FOLDER_ID": "folder",
            "YDBDOC_DOCS_ROOT": str(tmp_path),
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )
    result = main(
        ["translate", "--pr", "42", "--source-sha", services.source, "--budget-rub", "10"],
        dispatcher=runtime,
    )
    assert result == 0
    assert calls == []
    assert services.files["ydb/docs/en/core/page.md"] == b"# Translated\n"
    assert services.pr_exists
    assert len(services.comments) == 1
    assert services.comments[0]["body"].startswith("🟢 GREEN\n")
