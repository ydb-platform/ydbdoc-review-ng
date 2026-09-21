from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ydbdoc_review_ng.continuation import (
    ContinuationStage,
    ContinuationState,
    SourceChangeInventory,
    normalize_source_inventory,
)
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import ContentHash, GitSha, RepoPath
from ydbdoc_review_ng.persistence import YdbPersistence, ydb

NOW = datetime(2026, 9, 21, 9, tzinfo=UTC)


class CheckpointExecutor:
    """YDB boundary fake retains bound rows without implementing repository policy."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def execute(self, statement: str, parameters: Mapping[str, object], /):
        self.calls.append((statement, parameters))
        if "UPSERT" in statement:
            self.rows[str(parameters["continuation_id"])] = dict(parameters)
        elif "UPDATE" in statement:
            self.rows[str(parameters["continuation_id"])]["status"] = "closed"
        elif "SELECT" in statement:
            if "continuation_id" in parameters:
                row = self.rows.get(str(parameters["continuation_id"]))
                return [] if row is None else [row]
            return [
                row
                for row in self.rows.values()
                if parameters["pr_number"] in (row["source_pr"], row["trigger_pr"])
            ]
        return []


def checkpoint():
    return ydb.ContinuationCheckpoint(
        continuation_id="checkpoint-1",
        job_id="original-job",
        source_pr=42,
        trigger_pr=52,
        source_sha=GitSha("a" * 40),
        base_sha=GitSha("b" * 40),
        translation_branch="translation/pr-42",
        target_sha=None,
        source_inventory=SourceChangeInventory(()),
        scope_target_paths=(),
        state=ContinuationState(1, ContinuationStage.DIRECTION, None, None, (), (), (), None),
        created_at=NOW,
    )


def test_save_load_close_checkpoint_for_source_and_translation_pr() -> None:
    executor = CheckpointExecutor()
    store = YdbPersistence(executor)
    original = checkpoint()
    store.save_checkpoint(original, now=NOW)
    assert store.load_checkpoint(42, now=NOW) == original
    assert store.load_checkpoint(52, now=NOW) == original
    row = executor.rows["checkpoint-1"]
    assert row["job_id"] == "original-job"
    assert row["stage"] == "direction"
    assert row["target_sha"] is None
    assert row["status"] == "open"
    assert isinstance(row["state"], bytes)
    assert original.expires_at == datetime(2026, 10, 5, 9, tzinfo=UTC)
    store.close_checkpoint("checkpoint-1")
    with pytest.raises(ydb.PersistenceError):
        store.load_checkpoint(42, now=NOW)


def test_later_semantic_stop_keeps_original_creation_and_expiry() -> None:
    executor = CheckpointExecutor()
    store = YdbPersistence(executor)
    store.save_checkpoint(checkpoint(), now=NOW)
    later = NOW + timedelta(days=5)
    pending = ContinuationState(
        1,
        ContinuationStage.TRANSLATION,
        Direction.RU_TO_EN,
        ContentHash("d" * 64),
        (),
        (RepoPath("en/a.md"),),
        (),
        None,
    )
    store.save_checkpoint(
        replace(
            checkpoint(),
            created_at=later,
            target_sha=GitSha("c" * 40),
            state=pending,
            scope_target_paths=(RepoPath("en/a.md"),),
        ),
        now=later,
    )
    restored = store.load_checkpoint(52, now=later)
    assert restored.created_at == NOW
    assert restored.expires_at == datetime(2026, 10, 5, 9, tzinfo=UTC)
    assert restored.target_sha == GitSha("c" * 40)
    assert restored.job_id == "original-job"
    assert restored.state == pending
    assert restored.scope_target_paths == (RepoPath("en/a.md"),)
    assert executor.rows["checkpoint-1"]["scope_target_paths"] == b'["en/a.md"]'


@pytest.mark.parametrize("corruption", ["expired", "closed", "ambiguous", "state", "stage", "sha"])
def test_load_rejects_unusable_physically_present_rows(corruption: str) -> None:
    executor = CheckpointExecutor()
    store = YdbPersistence(executor)
    store.save_checkpoint(checkpoint(), now=NOW)
    row = executor.rows["checkpoint-1"]
    if corruption == "closed":
        row["status"] = "closed"
    elif corruption == "ambiguous":
        executor.rows["checkpoint-2"] = {**row, "continuation_id": "checkpoint-2"}
    elif corruption == "state":
        row["state"] = b'{"secret": "confidential source"}'
    elif corruption == "stage":
        row["stage"] = "review"
    elif corruption == "sha":
        row["source_sha"] = "confidential source"
    now = NOW + timedelta(days=14) if corruption == "expired" else NOW
    with pytest.raises(ydb.PersistenceError) as error:
        store.load_checkpoint(42, now=now)
    assert "confidential" not in str(error.value)


def test_closed_or_expired_lineage_cannot_be_reopened_or_extended() -> None:
    executor = CheckpointExecutor()
    store = YdbPersistence(executor)
    original = checkpoint()
    store.save_checkpoint(original, now=NOW)
    later = NOW + timedelta(days=14)
    with pytest.raises(ydb.PersistenceError):
        store.save_checkpoint(replace(original, created_at=later), now=later)
    store.close_checkpoint(original.continuation_id)
    with pytest.raises(ydb.PersistenceError):
        store.save_checkpoint(original, now=NOW)


def test_later_stop_can_associate_translation_pr_without_changing_lineage() -> None:
    executor = CheckpointExecutor()
    store = YdbPersistence(executor)
    original = replace(checkpoint(), trigger_pr=42)
    store.save_checkpoint(original, now=NOW)
    store.save_checkpoint(replace(original, trigger_pr=52), now=NOW + timedelta(hours=1))
    restored = store.load_checkpoint(52, now=NOW + timedelta(hours=1))
    assert restored.job_id == "original-job"
    assert restored.source_pr == 42
    assert restored.created_at == NOW
    assert store.load_checkpoint(42, now=NOW + timedelta(hours=1)) == restored


def test_checkpoint_errors_never_echo_state_payloads() -> None:
    class EchoingExecutor:
        def execute(self, statement, parameters, /):
            raise RuntimeError(f"confidential state: {parameters}")

    with pytest.raises(ydb.PersistenceError) as error:
        YdbPersistence(EchoingExecutor()).save_checkpoint(checkpoint(), now=NOW)
    assert "confidential" not in str(error.value)


def test_checkpoint_inventory_is_required_and_cannot_change_within_lineage() -> None:
    executor = CheckpointExecutor()
    store = YdbPersistence(executor)
    original = replace(
        checkpoint(),
        source_inventory=normalize_source_inventory(
            [
                {"filename": "ru/page.md", "status": "modified"},
            ]
        ),
    )
    store.save_checkpoint(original, now=NOW)
    row = executor.rows["checkpoint-1"]
    assert isinstance(row["source_inventory"], bytes)
    assert store.load_checkpoint(42, now=NOW).source_inventory == original.source_inventory
    changed = replace(
        original,
        source_inventory=normalize_source_inventory(
            [
                {"filename": "ru/page.md", "status": "added"},
            ]
        ),
    )
    with pytest.raises(ydb.PersistenceError, match="lineage mismatch"):
        store.save_checkpoint(changed, now=NOW)
    del row["source_inventory"]
    with pytest.raises(ydb.PersistenceError):
        store.load_checkpoint(42, now=NOW)


def selected_checkpoint(stage=ContinuationStage.TRANSLATION):
    from ydbdoc_review_ng.continuation import AcceptedMap

    path = RepoPath("en/a.md")
    review = stage is ContinuationStage.REVIEW
    state = ContinuationState(
        1,
        stage,
        Direction.RU_TO_EN,
        ContentHash("d" * 64),
        (AcceptedMap(path, (("field", "text"),)),) if review else (),
        () if review else (path,),
        (path,) if review else (),
        ContentHash("e" * 64) if review else None,
    )
    return replace(
        checkpoint(),
        state=state,
        scope_target_paths=(path,),
        target_sha=GitSha("c" * 40) if review else None,
    )


@pytest.mark.parametrize("stage", [ContinuationStage.TRANSLATION, ContinuationStage.REVIEW])
def test_checkpoint_scope_selection_requires_paths_for_selected_stages(stage):
    selected = selected_checkpoint(stage)
    with pytest.raises(ydb.PersistenceError):
        replace(selected, scope_target_paths=())
    with pytest.raises(ydb.PersistenceError):
        replace(selected, scope_target_paths=(RepoPath("en/foreign.md"),))
    with pytest.raises(ydb.PersistenceError):
        replace(checkpoint(), scope_target_paths=(RepoPath("en/a.md"),))


def test_scope_selection_is_frozen_after_direction_has_been_resolved():
    executor = CheckpointExecutor()
    store = YdbPersistence(executor)
    store.save_checkpoint(checkpoint(), now=NOW)
    selected = selected_checkpoint()
    store.save_checkpoint(selected, now=NOW)
    assert store.load_checkpoint(42, now=NOW) == selected
    store.save_checkpoint(selected_checkpoint(ContinuationStage.REVIEW), now=NOW)
    changed = replace(selected, scope_target_paths=(RepoPath("en/a.md"), RepoPath("en/noop.md")))
    with pytest.raises(ydb.PersistenceError, match="lineage mismatch"):
        store.save_checkpoint(changed, now=NOW)
    with pytest.raises(ydb.PersistenceError, match="lineage mismatch"):
        store.save_checkpoint(checkpoint(), now=NOW)


@pytest.mark.parametrize("wire", [None, b"[]", b'["en/a.md","en/a.md"]', b'["en/z.md","en/a.md"]'])
def test_persisted_scope_selection_corruption_fails_closed_on_load(wire):
    executor = CheckpointExecutor()
    store = YdbPersistence(executor)
    store.save_checkpoint(selected_checkpoint(), now=NOW)
    row = executor.rows["checkpoint-1"]
    if wire is None:
        del row["scope_target_paths"]
    else:
        row["scope_target_paths"] = wire
    with pytest.raises(ydb.PersistenceError):
        store.load_checkpoint(42, now=NOW)
