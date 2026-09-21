"""Public metadata regressions for valid flow YAML and fail-closed preflight."""

from decimal import Decimal

import pytest
from _runtime_services import RuntimeServices

from ydbdoc_review_ng.application import TranslateWorkflowInput, WorkflowError
from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.runtime import create_runtime
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError
from ydbdoc_review_ng.runtime_metadata import MetadataProducer

SOURCE = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
TARGET = SnapshotRef(SOURCE.repository, GitSha("b" * 40))
SOURCE_PATH = RepoPath("ydb/docs/ru/core/new.md")
TARGET_PATH = RepoPath("ydb/docs/en/core/new.md")


def producer(source_toc: bytes, target_toc: bytes = b"items:\n") -> MetadataProducer:
    class Reader:
        def read_bytes(self, snapshot, path):
            assert snapshot in {SOURCE, TARGET}
            if path.value.endswith("redirects.yaml"):
                return None
            assert path.value.endswith("toc.yaml")
            return source_toc if snapshot == SOURCE else target_toc

    return MetadataProducer(Reader(), SOURCE, TARGET, ())


@pytest.mark.parametrize(
    "source_toc",
    [
        b"items: [{name: New, href: new.md}]\n",
        b"items:\n  - name: Group\n    items: [{name: New, href: new.md}]\n",
        b"items: [{name: Group, items: [{name: New, href: new.md}]}]\n",
    ],
)
def test_flow_and_nested_source_mapping_adds_reachable_page(source_toc):
    changes = producer(source_toc).changes(SOURCE_PATH, TARGET_PATH, new=True)
    assert len(changes) == 1
    assert changes[0].path == RepoPath("ydb/docs/en/core/toc.yaml")
    assert b"href: new.md" in changes[0].after
    assert changes[0].before == b"items:\n"


def test_t017_f12_root_include_reaches_new_page_in_child_toc() -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/toc.yaml"): b"items: [{include: child/toc.yaml}]\n",
        (SOURCE, "ydb/docs/ru/core/child/toc.yaml"): (b"items:\n  - name: New\n    href: new.md\n"),
        (TARGET, "ydb/docs/en/core/toc.yaml"): b"items: [{include: child/toc.yaml}]\n",
        (TARGET, "ydb/docs/en/core/child/toc.yaml"): (
            b"items:\n  - name: Existing\n    href: existing.md\n"
        ),
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    changes = MetadataProducer(Reader(), SOURCE, TARGET, ()).changes(
        RepoPath("ydb/docs/ru/core/child/new.md"),
        RepoPath("ydb/docs/en/core/child/new.md"),
        new=True,
    )

    assert len(changes) == 1
    assert changes[0].path == RepoPath("ydb/docs/en/core/child/toc.yaml")
    assert changes[0].before == files[(TARGET, "ydb/docs/en/core/child/toc.yaml")]
    assert changes[0].after.count(b"href:") == 2
    assert b"href: new.md" in changes[0].after


def test_t017_b4_local_toc_include_cycle_is_rejected() -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/toc.yaml"): b"items: [{include: child/toc.yaml}]\n",
        (SOURCE, "ydb/docs/ru/core/child/toc.yaml"): (
            b"items: [{include: ../toc.yaml}, {href: new.md}]\n"
        ),
        (TARGET, "ydb/docs/en/core/toc.yaml"): b"items: [{include: child/toc.yaml}]\n",
        (TARGET, "ydb/docs/en/core/child/toc.yaml"): b"items:\n",
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    with pytest.raises(RuntimeBoundaryError, match="^unsupported_source_toc$"):
        MetadataProducer(Reader(), SOURCE, TARGET, ()).changes(
            RepoPath("ydb/docs/ru/core/child/new.md"),
            RepoPath("ydb/docs/en/core/child/new.md"),
            new=True,
        )


def test_flow_target_add_preserves_existing_bytes_and_nested_source_unreachable_is_noop():
    source = b"items: [{name: New, href: new.md}]\n"
    target = b"items: [{name: Existing, href: existing.md}] # retained\n"
    result = producer(source, target).changes(SOURCE_PATH, TARGET_PATH, new=True)
    assert len(result) == 1
    assert b"{name: Existing, href: existing.md}" in result[0].after
    assert result[0].after.endswith(b" # retained\n")
    assert b'"new.md"' in result[0].after
    assert (
        producer(b"items: [{items: [{href: other.md}]}]\n").changes(
            SOURCE_PATH, TARGET_PATH, new=True
        )
        == ()
    )


def test_compact_target_rename_preserves_neighbors_and_adds_direct_redirect():
    target = b'items: [{name: Old, href: "old.md"}, {href: other.md}]\n'
    changes = producer(b"items: [{href: new.md}]\n", target).changes(
        SOURCE_PATH, TARGET_PATH, old=RepoPath("ydb/docs/en/core/old.md")
    )
    assert changes[0].after == b'items: [{name: Old, href: "new.md"}, {href: other.md}]\n'
    assert changes[1].after == b'redirects:\n  - from: "core/old.md"\n    to: "core/new.md"\n'


@pytest.mark.parametrize(
    "source_toc",
    [
        b"items: [secret_payload\n",
        b"items: {name: secret_payload, href: new.md}\n",
        b"items: [{href: [secret_payload]}]\n",
        b"items: !unsupported [secret_payload]\n",
        b"items: [{href: old.md, href: new.md}]\n",
        b"items: &cycle [{items: *cycle}]\n",
    ],
)
def test_invalid_or_unsupported_source_toc_is_typed_non_echoing(source_toc):
    with pytest.raises(RuntimeBoundaryError, match="^unsupported_source_toc$") as caught:
        producer(source_toc).changes(SOURCE_PATH, TARGET_PATH, new=True)
    assert "secret_payload" not in str(caught.value)


@pytest.mark.parametrize(
    "source_toc",
    [
        b"items: [secret_payload\n",
        b"items: {name: secret_payload, href: page.md}\n",
    ],
)
def test_source_toc_preflight_precedes_every_model_and_github_mutation(source_toc):
    class Services(RuntimeServices):
        def github(self, method, path, payload):
            if path.endswith("/pulls/42/files?per_page=100"):
                return [{"status": "added", "filename": "ydb/docs/ru/core/page.md"}]
            return super().github(method, path, payload)

    services = Services()
    services.files["ydb/docs/ru/core/toc.yaml"] = source_toc
    services.files["ydb/docs/en/core/toc.yaml"] = b"items:\n"
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
    with pytest.raises(WorkflowError):
        runtime.doc_translate(TranslateWorkflowInput(42, GitSha(services.source), Decimal(10)))
    assert not any(method in {"MODEL", "POST", "PATCH"} for method, _ in services.events)
    assert [row["status"] for row in services.audit if "status" in row] == ["started", "failed"]


def test_mixed_direction_model_is_also_after_source_toc_preflight():
    class Services(RuntimeServices):
        def github(self, method, path, payload):
            if path.endswith("/pulls/42/files?per_page=100"):
                return [
                    {"status": "added", "filename": "ydb/docs/ru/core/page.md"},
                    {"status": "modified", "filename": "ydb/docs/en/core/other.md"},
                ]
            result = super().github(method, path, payload)
            if path.endswith("/pulls/42"):
                result["changed_files"] = 2
            return result

    services = Services()
    services.files["ydb/docs/ru/core/toc.yaml"] = b"items: {href: page.md}\n"
    services.files["ydb/docs/en/core/toc.yaml"] = b"items: []\n"
    services.files["ydb/docs/en/core/other.md"] = b"# Other\n"
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
    with pytest.raises(WorkflowError):
        runtime.doc_translate(TranslateWorkflowInput(42, GitSha(services.source), Decimal(10)))
    assert not any(method in {"MODEL", "POST", "PATCH"} for method, _ in services.events)
    assert services.audit[-1]["status"] == "failed"
