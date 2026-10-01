"""Strict, persistence-safe value model for continuation checkpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import cast

from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import ContentHash, GitSha, RepoPath
from ydbdoc_review_ng.plan import SourcePlan, validate_source_plan
from ydbdoc_review_ng.scope import ScopeManifest

__all__ = [
    "STATE_VERSION",
    "AcceptedDocument",
    "AcceptedMap",
    "ContinuationStage",
    "ContinuationState",
    "ContinuationStateError",
    "RestoredPlan",
    "SourceChange",
    "SourceChangeInventory",
    "SourceSemanticAction",
    "candidate_sha256",
    "checkpoint_scope_sha256",
    "decode_scope_target_paths",
    "decode_source_inventory",
    "decode_state",
    "encode_scope_target_paths",
    "encode_source_inventory",
    "encode_state",
    "normalize_source_inventory",
    "scope_sha256",
    "validate_restored_documents",
]

STATE_VERSION = 3
_STATE_KEYS = frozenset(
    {
        "state_version",
        "stage",
        "direction",
        "scope_sha256",
        "target_sha",
        "pending_paths",
        "review_paths",
    }
)


class ContinuationStateError(ValueError):
    """A persisted continuation state failed its closed schema or plan checks."""

    def __init__(self) -> None:
        super().__init__("invalid_continuation_state")


def _fail() -> ContinuationStateError:
    return ContinuationStateError()


def _exact(value: object, expected: type[object]) -> None:
    if type(value) is not expected:
        raise _fail()


class ContinuationStage(str, Enum):
    DIRECTION = "direction"
    TRANSLATION = "translation"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class SourceChange:
    """Only PR-file facts consumed by scope/metadata discovery, never patch bytes."""

    path: RepoPath
    status: str
    previous_path: RepoPath | None
    rename_changed: bool | None

    @property
    def operation(self) -> str:
        return {"added": "add", "modified": "modify", "removed": "delete", "renamed": "rename"}[
            self.status
        ]

    @property
    def old_path(self) -> RepoPath | None:
        return None if self.status == "added" else self.previous_path or self.path

    @property
    def new_path(self) -> RepoPath | None:
        return None if self.status == "removed" else self.path

    def __post_init__(self) -> None:
        _exact(self.path, RepoPath)
        _exact(self.status, str)
        if self.status not in {
            "added",
            "removed",
            "modified",
            "renamed",
            "copied",
            "changed",
            "unchanged",
        }:
            raise _fail()
        if self.previous_path is not None:
            _exact(self.previous_path, RepoPath)
        if any(
            len(path.value.encode("utf-8")) > 4096
            for path in (self.path, self.previous_path)
            if path is not None
        ):
            raise _fail()
        if self.status == "renamed":
            if self.previous_path is None or self.previous_path == self.path:
                raise _fail()
            _exact(self.rename_changed, bool)
        elif self.previous_path is not None or self.rename_changed is not None:
            raise _fail()


@dataclass(frozen=True, slots=True)
class SourceSemanticAction:
    """Exact classifier decision for one immutable Git operation."""

    path: RepoPath
    operation: str
    action: str
    toc_delta: str | None

    def __post_init__(self) -> None:
        _exact(self.path, RepoPath)
        _exact(self.operation, str)
        _exact(self.action, str)
        if self.operation not in {"add", "modify", "delete", "rename"}:
            raise _fail()
        if self.action not in {"page", "toc_delta", "resource", "none"}:
            raise _fail()
        if self.action == "toc_delta":
            if type(self.toc_delta) is not str or not self.toc_delta.strip():
                raise _fail()
        elif self.toc_delta is not None:
            raise _fail()


@dataclass(frozen=True, slots=True)
class SourceChangeInventory:
    """Complete Git inventory and diff provenance outside the eight-key state."""

    files: tuple[SourceChange, ...]
    source_base_sha: GitSha | None = None
    source_head_sha: GitSha | None = None
    semantic_actions: tuple[SourceSemanticAction, ...] = ()

    def __post_init__(self) -> None:
        _exact(self.files, tuple)
        if any(type(item) is not SourceChange for item in self.files):
            raise _fail()
        if (self.source_base_sha is None) != (self.source_head_sha is None):
            raise _fail()
        if self.source_base_sha is not None:
            _exact(self.source_base_sha, GitSha)
            _exact(self.source_head_sha, GitSha)
        paths = tuple(item.path.value for item in self.files)
        if paths != tuple(sorted(set(paths))):
            raise _fail()
        _exact(self.semantic_actions, tuple)
        if any(type(item) is not SourceSemanticAction for item in self.semantic_actions):
            raise _fail()
        if self.semantic_actions:
            self.require_semantic_actions()

    def require_semantic_actions(self) -> None:
        if tuple((item.path, item.operation) for item in self.semantic_actions) != tuple(
            (item.path, item.operation) for item in self.files
        ):
            raise _fail()


def normalize_source_inventory(files: Sequence[Mapping[str, object]], /) -> SourceChangeInventory:
    """Project the existing PR files response onto the exact inputs we consume."""
    try:
        changes = tuple(
            SourceChange(
                _path(raw["filename"]),
                cast(str, raw["status"]),
                _path(raw["previous_filename"]) if raw["status"] == "renamed" else None,
                raw.get("changes") != 0 if raw["status"] == "renamed" else None,
            )
            for raw in files
        )
        return SourceChangeInventory(tuple(sorted(changes, key=lambda item: item.path.value)))
    except (KeyError, TypeError, ValueError):
        raise _fail() from None


def encode_source_inventory(inventory: SourceChangeInventory, /) -> str:
    _exact(inventory, SourceChangeInventory)
    inventory.require_semantic_actions()
    if inventory.source_base_sha is None or inventory.source_head_sha is None:
        raise _fail()
    return json.dumps(
        {
            "source_base_sha": inventory.source_base_sha.value,
            "source_head_sha": inventory.source_head_sha.value,
            "files": [
                {
                    "path": item.path.value,
                    "status": item.status,
                    "previous_path": None
                    if item.previous_path is None
                    else item.previous_path.value,
                    "rename_changed": item.rename_changed,
                }
                for item in inventory.files
            ],
            "semantic_actions": [
                {
                    "path": item.path.value,
                    "operation": item.operation,
                    "action": item.action,
                    "toc_delta": item.toc_delta,
                }
                for item in inventory.semantic_actions
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_source_inventory(raw: str | bytes, /) -> SourceChangeInventory:
    if type(raw) not in {str, bytes}:
        raise _fail()
    try:
        parsed = json.loads(raw, object_pairs_hook=_ObjectPairs)
        envelope = dict(_pairs(parsed))
        if set(envelope) != {"files", "source_base_sha", "source_head_sha", "semantic_actions"}:
            raise _fail()
        rows = envelope["files"]
        _exact(rows, list)
        files = []
        for value in rows:
            values = dict(_pairs(value))
            if set(values) != {"path", "status", "previous_path", "rename_changed"}:
                raise _fail()
            files.append(
                SourceChange(
                    _path(values["path"]),
                    cast(str, values["status"]),
                    None if values["previous_path"] is None else _path(values["previous_path"]),
                    cast(bool | None, values["rename_changed"]),
                )
            )
        actions = envelope["semantic_actions"]
        _exact(actions, list)
        semantic_actions = []
        for value in actions:
            values = dict(_pairs(value))
            if set(values) != {"path", "operation", "action", "toc_delta"}:
                raise _fail()
            semantic_actions.append(
                SourceSemanticAction(
                    _path(values["path"]),
                    cast(str, values["operation"]),
                    cast(str, values["action"]),
                    cast(str | None, values["toc_delta"]),
                )
            )
        inventory = SourceChangeInventory(
            tuple(files),
            GitSha(envelope["source_base_sha"]),
            GitSha(envelope["source_head_sha"]),
            tuple(semantic_actions),
        )
        inventory.require_semantic_actions()
        return inventory
    except (KeyError, TypeError, ValueError):
        raise _fail() from None


def encode_scope_target_paths(paths: tuple[RepoPath, ...], /) -> str:
    """Encode the exact selected manifest order, including whole-file/no-op entries."""
    _exact_paths(paths)
    # Covers a full 100-file PR with the default 100 dependencies per article.
    if len(paths) > 10_100:
        raise _fail()
    values = tuple(path.value for path in paths)
    if values != tuple(sorted(values)) or any(
        len(value.encode("utf-8")) > 4096 for value in values
    ):
        raise _fail()
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4_000_000:
        raise _fail()
    return encoded


def decode_scope_target_paths(raw: str | bytes, /) -> tuple[RepoPath, ...]:
    if type(raw) not in {str, bytes} or len(raw) > 4_000_000:
        raise _fail()
    try:
        paths = _path_list(json.loads(raw))
        encode_scope_target_paths(paths)
        return paths
    except (KeyError, TypeError, ValueError, RecursionError):
        raise _fail() from None


@dataclass(frozen=True, slots=True)
class AcceptedMap:
    target_path: RepoPath
    fields: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _exact(self.target_path, RepoPath)
        _exact(self.fields, tuple)
        keys: list[str] = []
        for pair in self.fields:
            _exact(pair, tuple)
            if len(pair) != 2:
                raise _fail()
            key, value = pair
            _exact(key, str)
            _exact(value, str)
            if not key:
                raise _fail()
            keys.append(key)
        if len(keys) != len(set(keys)) or keys != sorted(keys):
            raise _fail()

    def as_dict(self) -> dict[str, str]:
        return dict(self.fields)


@dataclass(frozen=True, slots=True)
class AcceptedDocument:
    target_path: RepoPath
    translated_markdown: str = field(repr=False)

    def __post_init__(self) -> None:
        _exact(self.target_path, RepoPath)
        _exact(self.translated_markdown, str)


def _exact_paths(value: object) -> tuple[RepoPath, ...]:
    _exact(value, tuple)
    paths = cast(tuple[object, ...], value)
    if any(type(item) is not RepoPath for item in paths):
        raise _fail()
    result = cast(tuple[RepoPath, ...], paths)
    if len(result) != len(set(result)):
        raise _fail()
    return result


@dataclass(frozen=True, slots=True)
class ContinuationState:
    state_version: int
    stage: ContinuationStage
    direction: Direction | None
    scope_sha256: ContentHash | None
    target_sha: GitSha | None
    pending_paths: tuple[RepoPath, ...]
    review_paths: tuple[RepoPath, ...]

    def __post_init__(self) -> None:
        _exact(self.state_version, int)
        _exact(self.stage, ContinuationStage)
        if self.state_version != STATE_VERSION:
            raise _fail()
        if self.direction is not None:
            _exact(self.direction, Direction)
        if self.scope_sha256 is not None:
            _exact(self.scope_sha256, ContentHash)
        if self.target_sha is not None:
            _exact(self.target_sha, GitSha)
        pending = _exact_paths(self.pending_paths)
        review = _exact_paths(self.review_paths)

        if self.stage is ContinuationStage.DIRECTION:
            if any(
                (
                    self.direction is not None,
                    self.scope_sha256 is not None,
                    self.target_sha is not None,
                    bool(pending),
                    bool(review),
                )
            ):
                raise _fail()
        elif self.stage is ContinuationStage.TRANSLATION:
            if (
                self.direction is None
                or self.scope_sha256 is None
                or not pending
                or review
            ):
                raise _fail()
        elif self.stage is ContinuationStage.REVIEW:
            if (
                self.direction is None
                or self.scope_sha256 is None
                or self.target_sha is None
                or pending
                or not review
            ):
                raise _fail()
        else:
            raise _fail()


@dataclass(frozen=True, slots=True)
class RestoredPlan:
    target_path: RepoPath
    source: bytes = field(repr=False)
    plan: SourcePlan

    def __post_init__(self) -> None:
        _exact(self.target_path, RepoPath)
        _exact(self.source, bytes)
        _exact(self.plan, SourcePlan)
        try:
            validate_source_plan(self.source, self.plan)
        except (TypeError, ValueError):
            raise _fail() from None


class _ObjectPairs(list[tuple[object, object]]):
    pass


def _pairs(value: object) -> tuple[tuple[str, object], ...]:
    if type(value) is not _ObjectPairs:
        raise _fail()
    result: list[tuple[str, object]] = []
    keys: list[str] = []
    for raw_key, item in value:
        _exact(raw_key, str)
        key = cast(str, raw_key)
        keys.append(key)
        result.append((key, item))
    if len(keys) != len(set(keys)):
        raise _fail()
    return tuple(result)


def _enum(enum_type: type[ContinuationStage | Direction], value: object) -> object:
    _exact(value, str)
    try:
        return enum_type(cast(str, value))
    except ValueError:
        raise _fail() from None


def _optional_hash(value: object) -> ContentHash | None:
    if value is None:
        return None
    _exact(value, str)
    try:
        return ContentHash(cast(str, value))
    except ValueError:
        raise _fail() from None


def _path(value: object) -> RepoPath:
    _exact(value, str)
    try:
        return RepoPath(cast(str, value))
    except ValueError:
        raise _fail() from None


def _path_list(value: object) -> tuple[RepoPath, ...]:
    _exact(value, list)
    return tuple(_path(item) for item in cast(list[object], value))


def _optional_sha(value: object) -> GitSha | None:
    if value is None:
        return None
    _exact(value, str)
    try:
        return GitSha(cast(str, value))
    except ValueError:
        raise _fail() from None


def decode_state(raw: str | bytes, /) -> ContinuationState:
    """Decode state v3 while rejecting duplicate keys and all schema drift."""
    if type(raw) not in {str, bytes}:
        raise _fail()
    try:
        parsed = json.loads(raw, object_pairs_hook=_ObjectPairs)
        pairs = _pairs(parsed)
        if {key for key, _value in pairs} != _STATE_KEYS or len(pairs) != len(_STATE_KEYS):
            raise _fail()
        values = dict(pairs)
        version = values["state_version"]
        _exact(version, int)
        direction_value = values["direction"]
        direction = (
            None if direction_value is None else cast(Direction, _enum(Direction, direction_value))
        )
        return ContinuationState(
            cast(int, version),
            cast(ContinuationStage, _enum(ContinuationStage, values["stage"])),
            direction,
            _optional_hash(values["scope_sha256"]),
            _optional_sha(values["target_sha"]),
            _path_list(values["pending_paths"]),
            _path_list(values["review_paths"]),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError):
        raise _fail() from None


def encode_state(state: ContinuationState, /) -> str:
    """Encode a validated state using one deterministic JSON representation."""
    _exact(state, ContinuationState)
    payload = {
        "state_version": state.state_version,
        "stage": state.stage.value,
        "direction": None if state.direction is None else state.direction.value,
        "scope_sha256": None if state.scope_sha256 is None else state.scope_sha256.value,
        "target_sha": None if state.target_sha is None else state.target_sha.value,
        "pending_paths": [item.value for item in state.pending_paths],
        "review_paths": [item.value for item in state.review_paths],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def candidate_sha256(candidate: bytes, /) -> ContentHash:
    """Hash the exact candidate bytes that were published."""
    if type(candidate) is not bytes:
        raise TypeError("candidate must be exact bytes")
    return ContentHash(sha256(candidate).hexdigest())


def scope_sha256(manifest: ScopeManifest, /) -> ContentHash:
    """Hash the frozen direction and authoritative per-operation scope identity."""
    _exact(manifest, ScopeManifest)
    entries = [
        {
            "operation": entry.operation.value,
            "rename_from_target_path": (
                None
                if entry.rename_from_target_path is None
                else entry.rename_from_target_path.value
            ),
            "source_path": entry.pair.source_path.value,
            "target_path": entry.pair.target_path.value,
            "source_sha256": (
                None if entry.source_content is None else sha256(entry.source_content).hexdigest()
            ),
        }
        for entry in manifest.entries
    ]
    payload = {"direction": manifest.direction.value, "entries": entries}
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ContentHash(sha256(canonical.encode("utf-8")).hexdigest())


def checkpoint_scope_sha256(
    manifest: ScopeManifest,
    inventory: SourceChangeInventory,
    translation_plan_sha256: ContentHash | None = None,
    /,
) -> ContentHash:
    """Bind a checkpoint to exact scope, inventory and cross-file execution plan."""
    if translation_plan_sha256 is not None:
        _exact(translation_plan_sha256, ContentHash)
    canonical = json.dumps(
        {
            "scope_sha256": scope_sha256(manifest).value,
            "source_inventory": json.loads(encode_source_inventory(inventory)),
            "translation_plan_sha256": (
                None
                if translation_plan_sha256 is None
                else translation_plan_sha256.value
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ContentHash(sha256(canonical.encode("utf-8")).hexdigest())


def validate_restored_documents(
    state: ContinuationState,
    restored_plans: tuple[RestoredPlan, ...],
    /,
    *,
    metadata_paths: tuple[RepoPath, ...] = (),
) -> None:
    """Bind every pending/review path back to an authoritative source plan."""
    _exact(state, ContinuationState)
    _exact(restored_plans, tuple)
    if any(type(item) is not RestoredPlan for item in restored_plans):
        raise _fail()
    plans = {item.target_path: item for item in restored_plans}
    if len(plans) != len(restored_plans):
        raise _fail()
    referenced = set(state.pending_paths) | set(state.review_paths)
    metadata = set(_exact_paths(metadata_paths))
    if not referenced.issubset(set(plans) | metadata) or set(state.pending_paths) & metadata:
        raise _fail()
