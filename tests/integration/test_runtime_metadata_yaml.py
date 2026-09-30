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
            if path.value.endswith("/toc.yaml"):
                return source_toc if snapshot == SOURCE else target_toc
            return None

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


def test_ydb_toc_variant_adds_page_to_nearest_existing_target_toc() -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/toc_p.yaml"): (
            b"title: YDB\nitems:\n- include:\n  mode: link\n  path: toc_i.yaml\n"
        ),
        (SOURCE, "ydb/docs/ru/core/toc_i.yaml"): (
            b"items:\n- name: Development\n  href: dev/index.md\n  include:\n"
            b"    mode: link\n    path: dev/toc_p.yaml\n"
        ),
        (SOURCE, "ydb/docs/ru/core/dev/toc_p.yaml"): (
            b"items:\n- name: Optimization\n  href: optimization/index.md\n  include:\n"
            b"    mode: link\n    path: optimization/toc_p.yaml\n"
        ),
        (SOURCE, "ydb/docs/ru/core/dev/optimization/toc_p.yaml"): (
            b"items:\n- name: Optimizer hints\n  href: hints.md\n"
        ),
        (TARGET, "ydb/docs/en/core/dev/toc_p.yaml"): (
            b"items:\n- name: Query execution optimization\n"
            b"  href: query-execution-optimization/index.md\n"
        ),
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    changes = MetadataProducer(Reader(), SOURCE, TARGET, ()).changes(
        RepoPath("ydb/docs/ru/core/dev/optimization/hints.md"),
        RepoPath("ydb/docs/en/core/dev/optimization/hints.md"),
        new=True,
    )

    assert len(changes) == 1
    assert changes[0].path == RepoPath("ydb/docs/en/core/dev/toc_p.yaml")
    assert changes[0].before == files[(TARGET, "ydb/docs/en/core/dev/toc_p.yaml")]
    assert b"href: optimization/hints.md" in changes[0].after
    assert b"href: query-execution-optimization/index.md" in changes[0].after


def test_modified_page_repairs_missing_target_toc_entry_from_existing_target_h1() -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/toc_p.yaml"): (
            b"items:\n- include:\n    mode: link\n    path: maintenance/manual/toc_i.yaml\n"
        ),
        (SOURCE, "ydb/docs/ru/core/maintenance/manual/toc_i.yaml"): (
            b"items:\n- name: BlobDepot\n  href: blobdepot.md\n"
            b"- name: \xd0\x94\xd0\xb5\xd0\xba\xd0\xbe\xd0\xbc\xd0\xb8\xd1\x81\xd1\x81\xd0\xb8\xd1\x8f BlobDepot\n  href: blobdepot_decommit.md\n"
        ),
        (TARGET, "ydb/docs/en/core/maintenance/manual/toc_i.yaml"): (
            b"items:\n- name: BlobDepot\n  href: blobdepot.md\n"
        ),
        (TARGET, "ydb/docs/en/core/maintenance/manual/blobdepot_decommit.md"): (
            b"# Group Decommissioning\n\nExisting English article.\n"
        ),
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    changes = MetadataProducer(
        Reader(),
        SOURCE,
        TARGET,
        (RepoPath("ydb/docs/ru/core/maintenance/manual/toc_i.yaml"),),
    ).changes(
        RepoPath("ydb/docs/ru/core/maintenance/manual/blobdepot_decommit.md"),
        RepoPath("ydb/docs/en/core/maintenance/manual/blobdepot_decommit.md"),
    )

    assert len(changes) == 1
    assert changes[0].path == RepoPath("ydb/docs/en/core/maintenance/manual/toc_i.yaml")
    assert b"name: \"Group Decommissioning\"" in changes[0].after
    assert b"href: blobdepot_decommit.md" in changes[0].after


def test_modified_source_toc_repairs_existing_target_name() -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/toc_p.yaml"): (
            b"items:\n- include:\n    mode: link\n    path: maintenance/manual/toc_i.yaml\n"
        ),
        (SOURCE, "ydb/docs/ru/core/maintenance/manual/toc_i.yaml"): (
            b"items:\n- name: BlobDepot\n  href: blobdepot.md\n"
            b"- name: \xd0\x94\xd0\xb5\xd0\xba\xd0\xbe\xd0\xbc\xd0\xb8\xd1\x81\xd1\x81\xd0\xb8\xd1\x8f BlobDepot\n  href: blobdepot_decommit.md\n"
        ),
        (TARGET, "ydb/docs/en/core/maintenance/manual/toc_i.yaml"): (
            b"items:\n- name: BlobDepot\n  href: blobdepot.md\n"
            b"- name: BlobDepot decommit\n  href: blobdepot_decommit.md\n"
        ),
        (TARGET, "ydb/docs/en/core/maintenance/manual/blobdepot_decommit.md"): (
            b"# Group Decommissioning\n\nExisting English article.\n"
        ),
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    changes = MetadataProducer(
        Reader(),
        SOURCE,
        TARGET,
        (RepoPath("ydb/docs/ru/core/maintenance/manual/toc_i.yaml"),),
    ).changes(
        RepoPath("ydb/docs/ru/core/maintenance/manual/blobdepot_decommit.md"),
        RepoPath("ydb/docs/en/core/maintenance/manual/blobdepot_decommit.md"),
    )

    assert len(changes) == 1
    assert changes[0].path == RepoPath("ydb/docs/en/core/maintenance/manual/toc_i.yaml")
    assert b'name: "Group Decommissioning"' in changes[0].after
    assert b"name: BlobDepot decommit" not in changes[0].after


def test_pr50839_toc_translation_matches_exact_expected_file() -> None:
    source_toc_path = "ydb/docs/ru/core/maintenance/manual/toc_i.yaml"
    target_toc_path = "ydb/docs/en/core/maintenance/manual/toc_i.yaml"
    source_document = RepoPath(
        "ydb/docs/ru/core/maintenance/manual/blobdepot_decommit.md"
    )
    target_document = RepoPath(
        "ydb/docs/en/core/maintenance/manual/blobdepot_decommit.md"
    )
    target_before = (
        b"items:\n"
        b"- name: BlobDepot\n"
        b"  href: blobdepot.md\n"
        b"- name: BlobDepot decommit\n"
        b"  href: blobdepot_decommit.md\n"
    )
    expected = (
        b"items:\n"
        b"- name: BlobDepot\n"
        b"  href: blobdepot.md\n"
        b'- name: "Group Decommissioning"\n'
        b"  href: blobdepot_decommit.md\n"
    )
    files = {
        (SOURCE, source_toc_path): (
            b"items:\n"
            b"- name: BlobDepot\n"
            b"  href: blobdepot.md\n"
            b"- name: \xd0\x94\xd0\xb5\xd0\xba\xd0\xbe\xd0\xbc\xd0\xb8\xd1\x81\xd1\x81\xd0\xb8\xd1\x8f BlobDepot\n"
            b"  href: blobdepot_decommit.md\n"
        ),
        (TARGET, target_toc_path): target_before,
        (TARGET, target_document.value): b"# Group Decommissioning\n",
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    changes = MetadataProducer(
        Reader(), SOURCE, TARGET, (RepoPath(source_toc_path),)
    ).changes(source_document, target_document)

    # Exact bytes prove that href/order/other labels are preserved while the
    # changed localized navigation label is updated.
    assert len(changes) == 1
    assert changes[0].path == RepoPath(target_toc_path)
    assert changes[0].before == target_before
    assert changes[0].after == expected


def test_modified_source_toc_with_correct_target_name_is_a_noop() -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/maintenance/manual/toc_i.yaml"): (
            b"items:\n- name: \xd0\x94\xd0\xb5\xd0\xba\xd0\xbe\xd0\xbc\xd0\xb8\xd1\x81\xd1\x81\xd0\xb8\xd1\x8f BlobDepot\n  href: blobdepot_decommit.md\n"
        ),
        (TARGET, "ydb/docs/en/core/maintenance/manual/toc_i.yaml"): (
            b"items:\n- name: Group Decommissioning\n  href: blobdepot_decommit.md\n"
        ),
        (TARGET, "ydb/docs/en/core/maintenance/manual/blobdepot_decommit.md"): (
            b"# Group Decommissioning\n"
        ),
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    changes = MetadataProducer(
        Reader(), SOURCE, TARGET, (RepoPath("ydb/docs/ru/core/maintenance/manual/toc_i.yaml"),)
    ).changes(
        RepoPath("ydb/docs/ru/core/maintenance/manual/blobdepot_decommit.md"),
        RepoPath("ydb/docs/en/core/maintenance/manual/blobdepot_decommit.md"),
    )

    assert changes == ()


def test_delete_is_blocked_when_target_toc_would_keep_an_orphan() -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/toc.yaml"): b"items:\n",
        (TARGET, "ydb/docs/en/core/toc.yaml"): (
            b"items:\n- name: Deleted\n  href: deleted.md\n"
        ),
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    metadata = MetadataProducer(Reader(), SOURCE, TARGET, ())
    with pytest.raises(RuntimeBoundaryError, match="target_toc_reference_blocks_delete"):
        metadata.assert_target_document_unreferenced(
            RepoPath("ydb/docs/ru/core/deleted.md"),
            RepoPath("ydb/docs/en/core/deleted.md"),
        )


@pytest.mark.parametrize(
    "include",
    [
        "../../../outside/toc_p.yaml",
        "https://example.com/toc_p.yaml",
        "missing/toc_p.yaml",
        "toc_p.yaml",
    ],
)
def test_direct_ydb_toc_variant_rejects_unresolved_or_unsafe_include(include: str) -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/dev/optimization/toc_p.yaml"): (
            "items:\n- name: Optimizer hints\n  href: hints.md\n"
            f"- include: {include}\n"
        ).encode(),
        (TARGET, "ydb/docs/en/core/dev/toc_p.yaml"): b"items:\n",
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    with pytest.raises(RuntimeBoundaryError, match="^unsupported_source_toc$"):
        MetadataProducer(Reader(), SOURCE, TARGET, ()).changes(
            RepoPath("ydb/docs/ru/core/dev/optimization/hints.md"),
            RepoPath("ydb/docs/en/core/dev/optimization/hints.md"),
            new=True,
        )


@pytest.mark.parametrize(
    "include",
    [
        "../../../outside/toc_p.yaml",
        "https://example.com/toc_p.yaml",
        "missing/toc_p.yaml",
        "toc_p.yaml",
    ],
)
def test_direct_ydb_toc_variant_rejects_unsafe_include_before_rename(include: str) -> None:
    files = {
        (SOURCE, "ydb/docs/ru/core/dev/optimization/toc_p.yaml"): (
            "items:\n- href: index.md\n" f"- include: {include}\n"
        ).encode(),
        (TARGET, "ydb/docs/en/core/dev/optimization/toc_p.yaml"): (
            b"items:\n- href: old.md\n"
        ),
    }

    class Reader:
        def read_bytes(self, snapshot, path):
            return files.get((snapshot, path.value))

    with pytest.raises(RuntimeBoundaryError, match="^unsupported_source_toc$"):
        MetadataProducer(Reader(), SOURCE, TARGET, ()).changes(
            RepoPath("ydb/docs/ru/core/dev/optimization/new.md"),
            RepoPath("ydb/docs/en/core/dev/optimization/new.md"),
            old=RepoPath("ydb/docs/en/core/dev/optimization/old.md"),
        )


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
