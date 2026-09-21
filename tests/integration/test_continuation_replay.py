"""Replay crosses real HTTP/persistence/parser boundaries with ref-specific bytes."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from _runtime_services import RuntimeServices

from ydbdoc_review_ng.application import TranslateWorkflowInput
from ydbdoc_review_ng.continuation import (
    AcceptedMap,
    ContinuationStage,
    ContinuationState,
    ContinuationStateError,
    scope_sha256,
)
from ydbdoc_review_ng.domain import ContentHash, GitSha, RepoPath
from ydbdoc_review_ng.persistence import ContinuationCheckpoint, YdbPersistence
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import RuntimeContent, unpack
from ydbdoc_review_ng.runtime_github import GitHubBackend
from ydbdoc_review_ng.scope import FileOperation

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
RU = "ydb/docs/ru/core/"
EN = "ydb/docs/en/core/"
SOURCE = b"# Source\n\nSee https://source.test/original.\n\n```sql\nSELECT 1;\n```\n"
POISON = b"# Poison\n\nSee https://target.test/edited.\n\n```sql\nDROP TABLE t;\n```\n"
ENV = {
    "GITHUB_ACTOR": "writer",
    "YDBDOC_ALLOWED_ACTORS": "writer",
    "YANDEX_API_KEY": "test-key",
    "YANDEX_FOLDER_ID": "test-folder",
}


class ReplayServices(RuntimeServices):
    """Every contents request must name a known full SHA; trees really differ."""

    def __init__(self, *, merged=False):
        super().__init__()
        self.merged = merged
        self.inventory = [
            {"status": "modified", "filename": RU + name} for name in ("page.md", "pending.md")
        ]
        self.trees = {
            self.source: {RU + "page.md": SOURCE, RU + "pending.md": b"# Pending\n"},
            self.base: {RU + "page.md": b"# Base differs\n", RU + "pending.md": b"# Base\n"},
            self.translated: {EN + "page.md": POISON, EN + "pending.md": POISON},
            "d" * 40: {RU + "page.md": b"# Moved source\n"},
            "f" * 40: {RU + "page.md": b"# Moved base\n"},
        }
        if merged:
            self.trees[self.base] = dict(self.trees[self.source])
            self.trees[self.source] = {RU + "page.md": b"# Old merged PR version\n"}
        self.current_source = self.source
        self.current_base = self.base
        self.reads = []
        self.rows = {}
        self.direction_values = {}

    def github(self, method, path, payload):
        short = path.removeprefix("/repos/ydb-platform/ydb")
        if short.startswith("/contents/"):
            assert method == "GET"
            url = urlsplit(short)
            ref = parse_qs(url.query)["ref"][0]
            assert ref in self.trees, (ref, short)
            name = unquote(url.path.removeprefix("/contents/"))
            self.reads.append((ref, name))
            self.events.append((method, path))
            content = self.trees[ref].get(name)
            return (
                None
                if content is None
                else {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                }
            )
        if short == "/pulls/42":
            return {
                "number": 42,
                "body": "source",
                "merged": self.merged,
                "merge_commit_sha": self.source,
                "head": {
                    "sha": self.current_source,
                    "ref": "source",
                    "repo": {"full_name": "ydb-platform/ydb"},
                },
                "base": {"ref": "main", "repo": {"full_name": "ydb-platform/ydb"}},
                "changed_files": len(self.inventory),
            }
        if short == "/pulls/42/files?per_page=100":
            self.events.append((method, path))
            return self.inventory
        if short == "/git/ref/heads/main":
            self.events.append((method, path))
            return {"object": {"sha": self.current_base}}
        return super().github(method, path, payload)

    def execute(self, statement, parameters):
        self.audit.append(dict(parameters))
        if "continuations" in statement:
            if "UPSERT" in statement:
                self.rows[parameters["continuation_id"]] = dict(parameters)
            elif "SELECT" in statement:
                if "continuation_id" in parameters:
                    row = self.rows.get(parameters["continuation_id"])
                    return [] if row is None else [row]
                return list(self.rows.values())
        return []

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        properties = body["jsonSchema"]["schema"]["properties"]
        self.events.append(("MODEL", tuple(properties)))
        if "enum" in next(iter(properties.values())):
            values = {key: self.direction_values[key] for key in properties}
        else:
            raise AssertionError("replay preparation must not translate")
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


def runtime(services):
    source = RuntimeSource(ENV, GitHubBackend(services.github))
    store = YdbPersistence(services)
    models = RecordedModels(ENV, store, services.model)
    models.bind_job("original-job")
    return source, RuntimeContent(source, models, ENV), store


def frozen(services):
    source, content, store = runtime(services)
    sha = services.base if services.merged else services.source
    authorization = source.authorize_translate(TranslateWorkflowInput(42, GitSha(sha), Decimal(10)))
    snapshot = source.snapshot_translate(authorization)
    preparation = content.prepare_source(snapshot)
    plans = content.select_source(preparation)
    return source, content, store, plans


def accepted(document):
    return AcceptedMap(
        document.entry.pair.target_path,
        tuple(
            sorted(
                (field.field_id, field.text.replace("Source", "Translated"))
                for field in document.request.fields
            )
        ),
    )


def checkpoint(source, plans):
    from ydbdoc_review_ng.continuation import checkpoint_scope_sha256

    assert plans.manifest is not None
    page = next(
        document
        for document in plans.documents
        if document.entry.pair.target_path.value == EN + "page.md"
    )
    return ContinuationCheckpoint(
        continuation_id="replay",
        job_id="original-job",
        source_pr=42,
        trigger_pr=42,
        source_sha=source.snapshots.source_snapshot.commit_sha,
        base_sha=source.snapshots.translation_base_snapshot.commit_sha,
        translation_branch="translation/pr-42",
        target_sha=None,
        source_inventory=source.inventory,
        scope_target_paths=tuple(entry.pair.target_path for entry in plans.manifest.entries),
        state=ContinuationState(
            1,
            ContinuationStage.TRANSLATION,
            plans.manifest.direction,
            checkpoint_scope_sha256(plans.manifest, source.inventory),
            (accepted(page),),
            tuple(
                document.entry.pair.target_path
                for document in plans.documents
                if document is not page
                and document.entry.operation is not FileOperation.RENAME_TARGET
            ),
            (),
            None,
        ),
        created_at=NOW,
    )


def replay(content, saved):
    from ydbdoc_review_ng.runtime_continue import replay_continue

    return replay_continue(content, saved)


@pytest.mark.parametrize("merged", [False, True])
def test_replay_reads_saved_source_and_base_after_heads_and_pr_inventory_move(merged):
    services = ReplayServices(merged=merged)
    source, _, store, plans = frozen(services)
    saved = checkpoint(source, plans)
    store.save_checkpoint(saved, now=NOW)
    services.current_source, services.current_base = "d" * 40, "f" * 40
    services.inventory = [{"status": "added", "filename": RU + "unrelated.md"}]
    services.events.clear()
    services.reads.clear()
    _, content, store = runtime(services)
    restored = replay(content, store.load_checkpoint(42, now=NOW))
    assert restored.plans.manifest == plans.manifest
    assert restored.accepted_maps == saved.state.accepted_maps
    assert {document.entry.pair.target_path.value for document in restored.plans.documents} == {
        EN + "page.md",
        EN + "pending.md",
    }
    assert (
        next(
            document.source
            for document in restored.plans.documents
            if document.entry.pair.target_path.value == EN + "page.md"
        )
        == SOURCE
    )
    assert services.reads
    assert {ref for ref, _ in services.reads} <= {saved.source_sha.value, saved.base_sha.value}
    assert not any("/files?" in path or "/heads/main" in path for _, path in services.events)
    assert not any(method in {"MODEL", "POST", "PATCH"} for method, _ in services.events)


def test_replay_assembly_uses_source_protected_fragments_and_explicit_maps():
    services = ReplayServices()
    source, _, _, plans = frozen(services)
    saved = replace(checkpoint(source, plans), target_sha=GitSha(services.translated))
    services.branch_head = services.translated
    _, content, _ = runtime(services)
    services.reads.clear()
    restored = replay(content, saved)
    maps = restored.accepted_maps + tuple(
        accepted(document)
        for document in restored.plans.documents
        if document.entry.pair.target_path in saved.state.pending_paths
    )
    result = content.assemble(restored.plans, maps)
    assert unpack(result.content)[EN + "page.md"] == SOURCE.replace(b"Source", b"Translated")
    assert not any(ref == services.translated for ref, _ in services.reads)


@pytest.mark.parametrize(
    "corruption", ["digest", "inventory", "field", "missing_path", "status", "toc_seed"]
)
def test_replay_rejects_tampered_state_before_model_or_mutation(corruption):
    services = ReplayServices()
    source, _, _, plans = frozen(services)
    saved = checkpoint(source, plans)
    if corruption == "digest":
        saved = replace(saved, state=replace(saved.state, scope_sha256=ContentHash("0" * 64)))
    elif corruption == "inventory":
        saved = replace(
            saved,
            source_inventory=replace(
                saved.source_inventory, files=saved.source_inventory.files[:-1]
            ),
        )
    elif corruption == "status":
        saved = replace(
            saved,
            source_inventory=replace(
                saved.source_inventory,
                files=(
                    replace(saved.source_inventory.files[0], status="added"),
                    *saved.source_inventory.files[1:],
                ),
            ),
        )
    elif corruption == "toc_seed":
        from ydbdoc_review_ng.continuation import SourceChange

        saved = replace(
            saved,
            source_inventory=replace(
                saved.source_inventory,
                files=(
                    *saved.source_inventory.files,
                    SourceChange(RepoPath(RU + "toc.yaml"), "modified", None, None),
                ),
            ),
        )
    elif corruption == "field":
        saved = replace(
            saved,
            state=replace(
                saved.state,
                accepted_maps=(AcceptedMap(RepoPath(EN + "page.md"), (("foreign_field", "bad"),)),),
            ),
        )
    else:
        saved = replace(
            saved,
            state=replace(saved.state, pending_paths=(RepoPath(EN + "unknown.md"),)),
            scope_target_paths=(RepoPath(EN + "page.md"), RepoPath(EN + "unknown.md")),
        )
    _, content, _ = runtime(services)
    services.events.clear()
    with pytest.raises(ContinuationStateError):
        replay(content, saved)
    assert not any(method in {"MODEL", "POST", "PATCH"} for method, _ in services.events)


def test_mixed_complete_pair_stays_excluded_without_repeating_direction_call():
    services = ReplayServices()
    services.inventory += [
        {"status": "modified", "filename": locale + "complete.md"} for locale in (RU, EN)
    ]
    services.trees[services.source].update(
        {RU + "complete.md": b"# Complete RU\n", EN + "complete.md": b"# Complete EN\n"}
    )
    services.direction_values = {
        "complete.md": "complete_pair",
        "page.md": "ru_to_en",
        "pending.md": "ru_to_en",
    }
    source, _, _, plans = frozen(services)
    assert [method for method, _ in services.events].count("MODEL") == 1
    saved = checkpoint(source, plans)
    _, content, _ = runtime(services)
    services.events.clear()
    restored = replay(content, saved)
    assert scope_sha256(restored.plans.manifest) == scope_sha256(plans.manifest)
    assert {entry.pair.target_path.value for entry in restored.plans.manifest.entries} == {
        EN + "page.md",
        EN + "pending.md",
    }
    assert not any(method == "MODEL" for method, _ in services.events)


def test_replay_preserves_delete_rename_dependency_toc_redirect_and_absent_noop():
    services = ReplayServices()
    services.inventory += [
        {"status": "added", "filename": RU + "new.md"},
        {"status": "removed", "filename": RU + "deleted.md"},
        {"status": "removed", "filename": RU + "absent.md"},
        {
            "status": "renamed",
            "filename": RU + "moved.md",
            "previous_filename": RU + "old.md",
            "changes": 0,
        },
        {
            "status": "renamed",
            "filename": RU + "edited.md",
            "previous_filename": RU + "edited-old.md",
            "changes": 5,
        },
    ]
    tree = services.trees[services.source]
    tree[RU + "page.md"] = SOURCE + b"\n[Dependency](dep.md)\n"
    tree.update(
        {
            RU + "dep.md": b"# Dependency\n",
            RU + "new.md": b"# New\n",
            RU + "moved.md": b"# Original\n\n```sql\nSELECT 1;\n```\n",
            EN + "old.md": b"# Existing translation\n\n```sql\nSELECT 1;\n```\n",
            RU + "edited.md": SOURCE,
            EN + "edited-old.md": POISON,
            EN + "deleted.md": b"# Remove me\n",
            RU + "toc.yaml": b"items:\n  - name: new\n    href: new.md\n",
        }
    )
    services.trees[services.base][EN + "toc.yaml"] = (
        b"items:\n  - name: old\n    href: old.md\n  - name: edited\n    href: edited-old.md\n"
    )
    source, _, _, plans = frozen(services)
    saved = replace(checkpoint(source, plans), target_sha=GitSha(services.translated))
    services.branch_head = services.translated
    services.trees[services.translated][EN + "toc.yaml"] = b"invalid target yaml: ["
    services.trees[services.translated][EN + "old.md"] = POISON
    services.current_base = "f" * 40
    _, content, _ = runtime(services)
    services.reads.clear()
    restored = replay(content, saved)
    operations = {
        entry.pair.target_path.value: entry.operation for entry in restored.plans.manifest.entries
    }
    assert operations[EN + "deleted.md"] is FileOperation.DELETE_TARGET
    assert operations[EN + "absent.md"] is FileOperation.NOOP_TARGET_ABSENT
    assert operations[EN + "moved.md"] is FileOperation.RENAME_TARGET
    assert operations[EN + "edited.md"] is FileOperation.RENAME_TARGET_AND_TRANSLATE
    assert operations[EN + "dep.md"] is FileOperation.TRANSLATE
    maps = restored.accepted_maps + tuple(
        accepted(document)
        for document in restored.plans.documents
        if document.entry.pair.target_path in saved.state.pending_paths
    )
    candidate = content.assemble(restored.plans, maps)
    files = unpack(candidate.content)
    assert files[EN + "deleted.md"] is None
    assert EN + "absent.md" not in files
    assert files[EN + "old.md"] is None
    assert files[EN + "edited-old.md"] is None
    assert files[EN + "moved.md"] == b"# Existing translation\n\n```sql\nSELECT 1;\n```\n"
    assert files[EN + "edited.md"] == SOURCE.replace(b"Source", b"Translated")
    assert files[EN + "dep.md"] == b"# Dependency\n"
    assert b"href: new.md" in files[EN + "toc.yaml"]
    assert b"href: moved.md" in files[EN + "toc.yaml"]
    assert b"href: edited.md" in files[EN + "toc.yaml"]
    assert b"href: old.md" not in files[EN + "toc.yaml"]
    assert files["ydb/docs/en/redirects.yaml"] == (
        b'redirects:\n  - from: "core/edited-old.md"\n    to: "core/edited.md"\n'
        b'  - from: "core/old.md"\n    to: "core/moved.md"\n'
    )
    assert (services.base, EN + "toc.yaml") in services.reads
    assert not any(ref in {services.translated, "f" * 40} for ref, _ in services.reads)


def test_direction_stage_replay_returns_only_pinned_preparation_without_direction_call():
    services = ReplayServices()
    source, _, _, plans = frozen(services)
    saved = replace(
        checkpoint(source, plans),
        scope_target_paths=(),
        state=ContinuationState(
            1,
            ContinuationStage.DIRECTION,
            None,
            None,
            (),
            (),
            (),
            None,
        ),
    )
    services.current_source, services.current_base = "d" * 40, "f" * 40
    _, content, _ = runtime(services)
    services.events.clear()
    restored = replay(content, saved)
    assert restored.plans is None
    assert restored.accepted_maps == ()
    assert restored.preparation.inventories[0].ru.content == SOURCE
    assert not any(method in {"MODEL", "POST", "PATCH"} for method, _ in services.events)


def test_complete_pair_preparation_keeps_empty_candidate_noop():
    services = ReplayServices()
    services.inventory = [
        {"filename": locale + "page.md", "status": "modified"} for locale in (RU, EN)
    ]
    services.trees[services.source][EN + "page.md"] = SOURCE
    services.direction_values = {"page.md": "complete_pair"}
    _, content, _, plans = frozen(services)
    assert plans.manifest is None
    assert plans.documents == ()
    assert unpack(content.assemble(plans, ()).content) == {}


def test_shared_dependency_does_not_reselect_a_saved_complete_pair():
    services = ReplayServices()
    services.inventory += [
        {"status": "modified", "filename": locale + "complete.md"} for locale in (RU, EN)
    ]
    services.trees[services.source].update(
        {
            RU + "page.md": SOURCE + b"\n[shared](dep.md)\n",
            RU + "complete.md": b"# Complete RU\n\n[shared](dep.md)\n",
            EN + "complete.md": b"# Complete EN\n\n[shared](dep.md)\n",
            RU + "dep.md": b"# Dependency\n",
        }
    )
    services.direction_values = {
        "complete.md": "complete_pair",
        "page.md": "ru_to_en",
        "pending.md": "ru_to_en",
    }
    source, _, _, plans = frozen(services)
    saved = checkpoint(source, plans)
    _, content, _ = runtime(services)
    services.events.clear()
    restored = replay(content, saved)
    assert {entry.pair.target_path.value for entry in restored.plans.manifest.entries} == {
        EN + "page.md",
        EN + "pending.md",
        EN + "dep.md",
    }
    assert not any(method == "MODEL" for method, _ in services.events)


@pytest.mark.parametrize("tampered", [False, True])
def test_review_replay_checks_exact_reassembled_candidate_digest(tampered):
    from ydbdoc_review_ng.continuation import candidate_sha256
    from ydbdoc_review_ng.runtime_content import pack

    services = ReplayServices()
    source, _, _, plans = frozen(services)
    saved = checkpoint(source, plans)
    digest = candidate_sha256(
        pack(
            {
                EN + "page.md": SOURCE.replace(b"Source", b"Translated"),
                EN + "pending.md": b"# Pending\n",
            }
        )
    )
    saved = replace(
        saved,
        target_sha=GitSha(services.translated),
        state=replace(
            saved.state,
            stage=ContinuationStage.REVIEW,
            accepted_maps=tuple(accepted(document) for document in plans.documents),
            pending_paths=(),
            review_paths=(RepoPath(EN + "page.md"),),
            candidate_sha256=ContentHash("0" * 64) if tampered else digest,
        ),
    )
    services.branch_head = services.translated
    _, content, _ = runtime(services)
    services.events.clear()
    services.reads.clear()
    if tampered:
        with pytest.raises(ContinuationStateError):
            replay(content, saved)
    else:
        restored = replay(content, saved)
        assert (
            candidate_sha256(content.assemble(restored.plans, restored.accepted_maps).content)
            == digest
        )
    assert not any(method in {"MODEL", "POST", "PATCH"} for method, _ in services.events)
    assert not any(ref == services.translated for ref, _ in services.reads)


@pytest.mark.parametrize("verdict", ["complete_pair", "ru_to_en"])
@pytest.mark.parametrize("tampered", [False, True])
def test_renamed_complete_pair_restores_exact_excluded_or_selected_noop(verdict, tampered):
    services = ReplayServices()
    services.inventory += [
        {
            "status": "renamed",
            "filename": locale + "complete.md",
            "previous_filename": locale + "complete-old.md",
            "changes": 0,
        }
        for locale in (RU, EN)
    ]
    services.trees[services.source].update(
        {
            RU + "complete.md": b"# Complete RU\n",
            EN + "complete.md": b"# Complete EN\n",
        }
    )
    services.direction_values = {
        "complete.md": verdict,
        "page.md": "ru_to_en",
        "pending.md": "ru_to_en",
    }
    source, _, store, plans = frozen(services)
    expected_paths = (EN + "page.md", EN + "pending.md")
    if verdict == "ru_to_en":
        expected_paths = (EN + "complete.md", *expected_paths)
        assert plans.manifest.entries[0].operation is FileOperation.NOOP_TARGET_ALREADY_RENAMED
    assert tuple(entry.pair.target_path.value for entry in plans.manifest.entries) == expected_paths
    saved = checkpoint(source, plans)
    assert tuple(item.target_path.value for item in saved.state.accepted_maps) == (EN + "page.md",)
    assert tuple(path.value for path in saved.state.pending_paths) == (EN + "pending.md",)
    assert tuple(path.value for path in saved.scope_target_paths) == expected_paths
    store.save_checkpoint(saved, now=NOW)
    _, content, store = runtime(services)
    services.events.clear()
    saved = store.load_checkpoint(42, now=NOW)
    if tampered:
        alternate = (RepoPath(EN + "page.md"), RepoPath(EN + "pending.md"))
        if verdict == "complete_pair":
            alternate = (RepoPath(EN + "complete.md"), *alternate)
        with pytest.raises(ContinuationStateError):
            replay(content, replace(saved, scope_target_paths=alternate))
    else:
        restored = replay(content, saved)
        assert restored.plans.manifest == plans.manifest
    assert not any(
        method in {"MODEL", "POST", "PATCH", "PUT", "DELETE"} for method, _ in services.events
    )
