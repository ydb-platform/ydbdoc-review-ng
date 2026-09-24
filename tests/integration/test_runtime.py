"""The installed production composition, with only external I/O replaced."""

from __future__ import annotations

import base64
import importlib.util
import json
from decimal import Decimal

import pytest
from _runtime_services import RuntimeServices


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
    runtime = create_runtime(
        environment=environment,
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
    assert services.files["ydb/docs/en/core/page.md"] == b"# Translated\n"
    assert len(services.comments) == 1
    assert services.comments[0]["body"].startswith("GREEN\n")
    events = services.events
    assert (
        next(
            i
            for i, event in enumerate(events)
            if event == ("POST", "/repos/ydb-platform/ydb/git/refs")
        )
        < next(i for i, event in enumerate(events) if event == ("MODEL", ("verdict", "findings")))
        < next(
            i
            for i, event in enumerate(events)
            if event == ("POST", "/repos/ydb-platform/ydb/pulls")
        )
    )
    services.events = []
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
        ("MODEL", ("verdict", "findings"))
    ]
    assert not any(
        "/git/" in path and method in {"POST", "PATCH"} for method, path in services.events
    )
    assert len(services.comments) == 1
    assert (
        sum(row.get("status") == "succeeded" for row in services.audit) == 5
    )  # two jobs, three model attempts


class ContentFilterServices(RuntimeServices):
    def __init__(self, filtered_responses: int) -> None:
        super().__init__()
        self.filtered_responses = filtered_responses
        self.raw_request_bodies: list[bytes] = []

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        if body.get("jsonSchema") is None and not body["messages"][-1]["text"].startswith(
            "Repair"
        ):
            self.raw_request_bodies.append(request.body)
            response = super().model(request)
            if len(self.raw_request_bodies) <= self.filtered_responses:
                document = json.loads(response.body)
                document["result"]["alternatives"][0][
                    "status"
                ] = "ALTERNATIVE_STATUS_CONTENT_FILTER"
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
    translation_attempts = [
        row for row in attempts if row["role"] == "translate"
    ]
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
    assert sum(
        method in {"POST", "PATCH"} and "/git/refs" in path
        for method, path in services.events
    ) == 1


def test_runtime_two_content_filters_fail_without_publication_or_checkpoint() -> None:
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
        row
        for row in services.audit
        if "attempt_id" in row and row["role"] == "translate"
    ]
    assert exit_code == 1
    assert len(services.raw_request_bodies) == 2
    assert services.raw_request_bodies[0] == services.raw_request_bodies[1]
    assert [row["status"] for row in translation_attempts] == ["failed", "failed"]
    assert [row["error"] for row in translation_attempts] == [
        "content_filter",
        "content_filter",
    ]
    assert [row["cost_rub"] for row in translation_attempts] == [
        Decimal("0.01"),
        Decimal("0.01"),
    ]
    assert not any(
        method in {"POST", "PATCH"} for method, _path in services.events
    )
    assert all("continuation_id" not in row for row in services.audit)
    assert services.audit[-1]["status"] == "failed"


class AdaptiveContentFilterServices(ContentFilterServices):
    def __init__(self, filtered_responses: int) -> None:
        super().__init__(filtered_responses)
        lengths = [157] * 49 + [177] + [130] * 60 + [131]
        parts = []
        for number, length in enumerate(lengths):
            prefix = f"## Block {number:03d} "
            parts.append(prefix + "x" * (length - len(prefix) - 1) + "\n")
        self.files["ydb/docs/ru/core/page.md"] = "".join(parts).encode()


def test_runtime_adaptive_split_audits_parent_and_children_then_publishes_once() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = AdaptiveContentFilterServices(filtered_responses=2)
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
        row
        for row in services.audit
        if "attempt_id" in row and row["role"] == "translate"
    ]
    assert exit_code == 0
    assert len(services.raw_request_bodies) == 4
    assert services.raw_request_bodies[0] == services.raw_request_bodies[1]
    assert [row["status"] for row in translation_attempts] == [
        "failed",
        "failed",
        "succeeded",
        "succeeded",
    ]
    assert [row["error"] for row in translation_attempts] == [
        "content_filter",
        "content_filter",
        None,
        None,
    ]
    assert [row["cost_rub"] for row in translation_attempts] == [Decimal("0.01")] * 4
    assert sum(
        method in {"POST", "PATCH"} and "/git/refs" in path
        for method, path in services.events
    ) == 1


def test_runtime_filtered_child_is_terminal_without_publication_or_further_split() -> None:
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    services = AdaptiveContentFilterServices(filtered_responses=4)
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
        row
        for row in services.audit
        if "attempt_id" in row and row["role"] == "translate"
    ]
    assert exit_code == 1
    assert len(services.raw_request_bodies) == 4
    assert services.raw_request_bodies[0] == services.raw_request_bodies[1]
    assert services.raw_request_bodies[2] == services.raw_request_bodies[3]
    assert services.raw_request_bodies[0] != services.raw_request_bodies[2]
    assert [row["error"] for row in translation_attempts] == ["content_filter"] * 4
    assert [row["cost_rub"] for row in translation_attempts] == [Decimal("0.01")] * 4
    assert not any(method in {"POST", "PATCH"} for method, _path in services.events)
    assert all("continuation_id" not in row for row in services.audit)


def test_t017_f04_pure_rename_rejects_changed_whole_fence_before_commit() -> None:
    from ydbdoc_review_ng.application import TranslateWorkflowInput, WorkflowError
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.runtime import create_runtime

    class RenameServices(RuntimeServices):
        def github(self, method, path, payload):
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

    assert not any(method in {"POST", "PATCH", "MODEL"} for method, _ in services.events)
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
        schema_wrapper = body.get("jsonSchema")
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
    assert (("MODEL", ("verdict", "findings")) in services.events) is expects_critic
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
    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        schema_wrapper = body.get("jsonSchema")
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
        self.events.append(("MODEL", properties))
        if properties == ("page.md",):
            values = {"page.md": "ru_to_en"}
        elif "verdict" in properties:
            values = {"verdict": "GREEN", "findings": []}
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
    assert ("MODEL", ("verdict", "findings")) in services.events
    assert services.comments[0]["body"].startswith("GREEN\n")


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
        ("MODEL", ("verdict", "findings"))
    ]
    assert services.comments[0]["body"].startswith("GREEN\n")


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
    assert boundary.call_count == 1
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error"] == "transport"
    assert attempts[0]["response"] == partial
    assert attempts[0]["cost_rub"] is None
    assert services.audit[-1]["status"] == "failed"
    assert services.audit[-1]["error"] == "prepare_failed"
    assert not any(method in {"POST", "PATCH"} for method, _path in services.events)


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
    assert boundary.call_count == 1
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error"] == "transport"
    assert attempts[0]["response"] == partial
    assert attempts[0]["cost_rub"] is None
    assert services.audit[-1]["status"] == "failed"
    assert services.audit[-1]["error"] == "prepare_failed"
    assert not any(method in {"POST", "PATCH"} for method, _path in services.events)


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


@pytest.mark.parametrize("operation", ["added", "removed", "renamed"])
def test_runtime_canonical_file_operations_have_shipped_producer(operation):
    from ydbdoc_review_ng.application import TranslateWorkflowInput
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.runtime import create_runtime

    class Services(RuntimeServices):
        def github(self, method, path, payload):
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
    assert len(model_calls) == {"added": 2, "removed": 0, "renamed": 1}[operation]
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


def test_model_repair_is_published_before_final_critic_and_only_then_pr():
    from ydbdoc_review_ng.application import TranslateWorkflowInput
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.models import HttpResponse
    from ydbdoc_review_ng.runtime import create_runtime

    class Services(RuntimeServices):
        def __init__(self):
            super().__init__()
            self.field_id = None
            self.critics = 0

        def model(self, request):
            body = json.loads(request.body)
            schema_wrapper = body.get("jsonSchema")
            if schema_wrapper is None:
                prompt = body["messages"][-1]["text"]
                if prompt.startswith("Repair"):
                    self.events.append(("REPAIR", "markdown"))
                    current = prompt.split("<current-target>\n", 1)[1].split(
                        "</current-target>", 1
                    )[0]
                    values = current.replace("# Translated", "# Corrected", 1)
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
                                                "text": values,
                                            },
                                        }
                                    ]
                                }
                            }
                        ).encode(),
                        Decimal("0.02"),
                    )
                return super().model(request)
            schema = schema_wrapper["schema"]
            self.critics += 1
            field_ids = schema["properties"]["findings"]["items"]["properties"][
                "field_ids"
            ]["items"]["enum"]
            self.field_id = field_ids[0]
            if self.critics > 1:
                return super().model(request)
            self.events.append(("CRITIC", "first"))
            values = {
                "verdict": "RED",
                "findings": [
                    {
                        "repairable": True,
                        "reason": "Wrong term",
                        "expected_correction": "Use Corrected",
                        "searchable_snippet": "Translated",
                        "target_path": "ydb/docs/en/core/page.md",
                        "target_line": 1,
                        "field_ids": [self.field_id],
                    }
                ],
            }
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
                            ]
                        }
                    }
                ).encode(),
                Decimal("0.02"),
            )

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
    assert significant == ["MODEL", "commits", "CRITIC", "REPAIR", "commits", "MODEL", "pulls"]
    assert "Current job cost: 0.06 RUB" in services.comments[0]["body"]


def test_runtime_never_reports_green_after_branch_moves_during_critic():
    from ydbdoc_review_ng.cli import main
    from ydbdoc_review_ng.runtime import create_runtime

    class Services(RuntimeServices):
        def model(self, request):
            response = super().model(request)
            schema_wrapper = json.loads(request.body).get("jsonSchema")
            if schema_wrapper and "verdict" in schema_wrapper["schema"]["properties"]:
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
