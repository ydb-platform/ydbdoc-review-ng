"""Replay crosses real HTTP/persistence/parser boundaries with ref-specific bytes."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from _runtime_services import (
    RuntimeServices,
    classification_response,
    request_prompt,
    request_schema,
    seed_inventory_preimages,
)

from ydbdoc_review_ng.application import TranslateWorkflowInput
from ydbdoc_review_ng.continuation import (
    STATE_VERSION,
    AcceptedDocument,
    AcceptedMap,
    ContinuationStage,
    ContinuationState,
    ContinuationStateError,
    SourceSemanticAction,
    scope_sha256,
)
from ydbdoc_review_ng.domain import ContentHash, GitSha, Mode, RepoPath
from ydbdoc_review_ng.persistence import (
    ContinuationCheckpoint,
    JobStatus,
    YdbPersistence,
    semantic_stop_error,
)
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
            self.trees[self.source] = {
                RU + "page.md": b"# Old merged PR version\n", RU + "pending.md": b"# Old pending\n",
            }
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
                "base": {
                    "ref": "main",
                    "sha": self.base,
                    "repo": {"full_name": "ydb-platform/ydb"},
                },
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
        if "/jobs`" in statement and "SELECT" in statement:
            row = {}
            for audit in self.audit:
                if (
                    audit.get("job_id") == parameters["job_id"]
                    and "role" not in audit
                    and "continuation_id" not in audit
                ):
                    row.update(audit)
            return [row] if row else []
        self.audit.append(dict(parameters))
        if "continuations" in statement:
            if "UPSERT" in statement:
                self.rows[parameters["continuation_id"]] = dict(parameters)
            elif "SET status = 'open'" in statement:
                row = self.rows[parameters["continuation_id"]]
                if all(row.get(key) == value for key, value in parameters.items()):
                    row["status"] = "open"
            elif "SELECT" in statement:
                if "continuation_id" in parameters:
                    row = self.rows.get(parameters["continuation_id"])
                    return [] if row is None else [row]
                return list(self.rows.values())
        return []

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        properties = request_schema(body)["schema"]["properties"]
        self.events.append(("MODEL", tuple(properties)))
        if "translation_required" in properties:
            direction = "en_to_ru" if all(item["filename"].startswith(EN)
                                           for item in self.inventory) else "ru_to_en"
            values = classification_response(request_prompt(body), direction=direction,
                                             decisions=self.direction_values)
        else:
            raise AssertionError("replay preparation must not translate")
        return HttpResponse(
            200,
            json.dumps(
                {
                    "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": json.dumps(values)},
                            }
                        ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
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
    seed_inventory_preimages(services.inventory, services.trees[services.base], services.trees[services.source])
    source, content, store = runtime(services)
    sha = services.base if services.merged else services.source
    authorization = source.authorize_translate(TranslateWorkflowInput(42, GitSha(sha), Decimal(10)))
    snapshot = source.snapshot_translate(authorization)
    preparation = content.prepare_source(snapshot)
    plans = content.select_source(preparation)
    return source, content, store, plans


def accepted_map(document):
    return AcceptedMap(
        document.entry.pair.target_path,
        tuple(
            sorted(
                (field.field_id, field.text.replace("Source", "Translated"))
                for field in document.request.fields
            )
        ),
    )


def accepted(document):
    values = accepted_map(document).as_dict()
    from ydbdoc_review_ng.translation import assemble_candidate

    translated = assemble_candidate(
        document.source, document.plan, document.request, values
    ).decode("utf-8")
    return AcceptedDocument(document.entry.pair.target_path, translated)


def checkpoint(source, plans, *, published_page: bytes | None = None, published_sha: str | None = None):
    from ydbdoc_review_ng.continuation import checkpoint_scope_sha256
    from ydbdoc_review_ng.translation_plan import translation_plan_sha256

    assert plans.manifest is not None
    page = next(
        document
        for document in plans.documents
        if document.entry.pair.target_path.value.endswith("/page.md")
    )
    pending = tuple(
        document.entry.pair.target_path
        for document in plans.documents
        if document is not page and document.entry.operation is not FileOperation.RENAME_TARGET
    )
    if published_page is None:
        target_sha = None
        pending_paths = pending + (page.entry.pair.target_path,)
    else:
        assert published_sha is not None
        target_sha = GitSha(published_sha)
        pending_paths = pending
    return ContinuationCheckpoint(
        continuation_id="replay",
        job_id="original-job",
        source_pr=42,
        trigger_pr=42,
        source_sha=source.snapshots.source_snapshot.commit_sha,
        base_sha=source.snapshots.translation_base_snapshot.commit_sha,
        translation_branch="translation/pr-42",
        target_sha=target_sha,
        source_inventory=plans.preparation.inventory,
        scope_target_paths=tuple(entry.pair.target_path for entry in plans.manifest.entries),
        state=ContinuationState(
            STATE_VERSION,
            ContinuationStage.TRANSLATION,
            plans.manifest.direction,
            checkpoint_scope_sha256(
                plans.manifest,
                plans.preparation.inventory,
                translation_plan_sha256(plans.translation_plan),
            ),
            target_sha,
            pending_paths,
            (),
        ),
        created_at=NOW,
    )


def replay(content, saved):
    from ydbdoc_review_ng.runtime_continue import replay_continue

    return replay_continue(content, saved)


def save_semantic(store, saved):
    job_id = store.start_job(
        Mode.DOC_TRANSLATE,
        pr_number=saved.source_pr,
        source_sha=saved.source_sha.value,
        target_sha=None,
        started_at=NOW,
    )
    pending = store.save_checkpoint(replace(saved, job_id=job_id), now=NOW)
    store.finish_job(
        job_id,
        JobStatus.FAILED,
        error=semantic_stop_error(saved.state.stage),
        finished_at=NOW,
        target_sha=None if saved.target_sha is None else saved.target_sha.value,
    )
    return store.activate_checkpoint(pending, now=NOW)


@pytest.mark.parametrize("merged", [False, True])
def test_replay_reads_saved_source_and_base_after_heads_and_pr_inventory_move(merged):
    services = ReplayServices(merged=merged)
    source, _, store, plans = frozen(services)
    page = next(
        document
        for document in plans.documents
        if document.entry.pair.target_path.value.endswith("/page.md")
    )
    published = accepted(page).translated_markdown.encode()
    services.trees[services.translated][page.entry.pair.target_path.value] = published
    saved = checkpoint(
        source, plans, published_page=published, published_sha=services.translated
    )
    services.branch_head = services.translated
    saved = save_semantic(store, saved)
    services.current_source, services.current_base = "d" * 40, "f" * 40
    services.inventory = [{"status": "added", "filename": RU + "unrelated.md"}]
    services.events.clear()
    services.reads.clear()
    _, content, store = runtime(services)
    restored = replay(content, store.load_checkpoint(42, now=NOW))
    assert restored.plans.manifest == plans.manifest
    assert tuple(item.target_path.value for item in restored.accepted_documents) == (
        page.entry.pair.target_path.value,
    )
    assert restored.accepted_documents[0].translated_markdown.encode() == published
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
    assert {ref for ref, _ in services.reads} <= {
        saved.source_sha.value,
        saved.base_sha.value,
        services.translated,
    }
    assert not any("/files?" in path or "/heads/main" in path for _, path in services.events)
    assert not any(method in {"MODEL", "POST", "PATCH"} for method, _ in services.events)


def test_en_to_ru_replay_restores_complete_document_and_only_pending_path():
    services = ReplayServices()
    services.inventory = [
        {"status": "modified", "filename": EN + name}
        for name in ("page.md", "pending.md")
    ]
    services.trees[services.source] = {
        EN + "page.md": SOURCE,
        EN + "pending.md": b"# Pending\n",
    }
    services.trees[services.base] = {
        RU + "page.md": b"# Base differs\n",
        RU + "pending.md": b"# Base\n",
    }
    source, _, store, plans = frozen(services)
    page = next(
        document
        for document in plans.documents
        if document.entry.pair.target_path.value.endswith("/page.md")
    )
    published = accepted(page).translated_markdown.encode()
    services.trees[services.translated][page.entry.pair.target_path.value] = published
    services.branch_head = services.translated
    saved = save_semantic(
        store,
        checkpoint(source, plans, published_page=published, published_sha=services.translated),
    )

    _, content, store = runtime(services)
    restored = replay(content, store.load_checkpoint(42, now=NOW))

    assert restored.plans.manifest.direction.value == "en_to_ru"
    assert tuple(item.target_path.value for item in restored.accepted_documents) == (
        RU + "page.md",
    )
    assert tuple(
        entry.pair.target_path.value for entry in restored.plans.manifest.entries
    ) == (
        RU + "page.md",
        RU + "pending.md",
    )
    assert saved.state.pending_paths == (RepoPath(RU + "pending.md"),)


def test_replay_assembly_uses_source_protected_fragments_and_explicit_maps():
    services = ReplayServices()
    source, _, _, plans = frozen(services)
    page = next(
        document
        for document in plans.documents
        if document.entry.pair.target_path.value.endswith("/page.md")
    )
    published = accepted(page).translated_markdown.encode()
    services.trees[services.translated][page.entry.pair.target_path.value] = published
    services.branch_head = services.translated
    saved = checkpoint(
        source, plans, published_page=published, published_sha=services.translated
    )
    _, content, _ = runtime(services)
    services.reads.clear()
    restored = replay(content, saved)
    maps = restored.accepted_maps + tuple(
        accepted_map(document)
        for document in restored.plans.documents
        if document.entry.pair.target_path in saved.state.pending_paths
    )
    result = content.assemble(restored.plans, maps)
    assert unpack(result.content)[EN + "page.md"] == SOURCE.replace(b"Source", b"Translated")
    assert any(ref == services.translated for ref, _ in services.reads)


@pytest.mark.parametrize(
    "corruption", ["digest", "inventory", "document", "missing_path", "status", "toc_seed"]
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
                saved.source_inventory, files=saved.source_inventory.files[:-1],
                semantic_actions=saved.source_inventory.semantic_actions[:-1],
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
                semantic_actions=(replace(saved.source_inventory.semantic_actions[0], operation="add"),
                                  *saved.source_inventory.semantic_actions[1:]),
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
                semantic_actions=(*saved.source_inventory.semantic_actions,
                    SourceSemanticAction(RepoPath(RU + "toc.yaml"), "modify", "toc_delta", "Extra entry.")),
            ),
        )
    elif corruption == "document":
        services.branch_head = services.translated
        del services.trees[services.translated][EN + "page.md"]
        pending = tuple(
            path for path in saved.state.pending_paths if not path.value.endswith("/page.md")
        )
        saved = replace(
            saved,
            target_sha=GitSha(services.translated),
            state=replace(
                saved.state,
                target_sha=GitSha(services.translated),
                pending_paths=pending,
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
        accepted_map(document)
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
            STATE_VERSION,
            ContinuationStage.DIRECTION,
            None,
            None,
            None,
            (),
            (),
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
def test_review_replay_loads_candidate_from_branch_target_sha(tampered):
    services = ReplayServices()
    source, _, _, plans = frozen(services)
    files = {
        document.entry.pair.target_path.value: accepted(document).translated_markdown.encode()
        for document in plans.documents
    }
    services.trees[services.translated].update(files)
    if tampered:
        del services.trees[services.translated][EN + "page.md"]
    services.branch_head = services.translated
    scope = checkpoint(source, plans).state.scope_sha256
    saved = replace(
        checkpoint(source, plans),
        target_sha=GitSha(services.translated),
        state=ContinuationState(
            STATE_VERSION,
            ContinuationStage.REVIEW,
            plans.manifest.direction,
            scope,
            GitSha(services.translated),
            (),
            (RepoPath(EN + "page.md"),),
        ),
    )
    _, content, _ = runtime(services)
    services.events.clear()
    services.reads.clear()
    if tampered:
        with pytest.raises(ContinuationStateError):
            replay(content, saved)
    else:
        restored = replay(content, saved)
        assembled = content.assemble_documents(
            restored.plans,
            restored.accepted_documents,
            restored.accepted_maps,
        )
        assert unpack(assembled.content)[EN + "page.md"] == files[EN + "page.md"]
    assert not any(method in {"MODEL", "POST", "PATCH"} for method, _ in services.events)
    assert any(ref == services.translated for ref, _ in services.reads)


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
    # Bilateral renames stay in scope as already-renamed no-ops. Direction-only
    # inventory classification cannot exclude them via a per-pair complete verdict.
    expected_paths = (EN + "complete.md", EN + "page.md", EN + "pending.md")
    assert plans.manifest.entries[0].operation is FileOperation.NOOP_TARGET_ALREADY_RENAMED
    assert tuple(entry.pair.target_path.value for entry in plans.manifest.entries) == expected_paths
    page = next(
        document
        for document in plans.documents
        if document.entry.pair.target_path.value.endswith("/page.md")
    )
    published = accepted(page).translated_markdown.encode()
    services.trees[services.translated][page.entry.pair.target_path.value] = published
    services.branch_head = services.translated
    saved = checkpoint(
        source, plans, published_page=published, published_sha=services.translated
    )
    assert saved.state.pending_paths == (RepoPath(EN + "pending.md"),)
    assert tuple(path.value for path in saved.scope_target_paths) == expected_paths
    saved = save_semantic(store, saved)
    _, content, store = runtime(services)
    services.events.clear()
    saved = store.load_checkpoint(42, now=NOW)
    if tampered:
        with pytest.raises(ContinuationStateError):
            replay(
                content,
                replace(
                    saved,
                    scope_target_paths=(RepoPath(EN + "page.md"), RepoPath(EN + "pending.md")),
                ),
            )
    else:
        restored = replay(content, saved)
        assert restored.plans.manifest == plans.manifest
    assert not any(
        method in {"MODEL", "POST", "PATCH", "PUT", "DELETE"} for method, _ in services.events
    )
