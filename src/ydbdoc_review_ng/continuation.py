"""Strict, persistence-safe value model for continuation checkpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import cast

from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import ContentHash, RepoPath
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

STATE_VERSION = 2
_STATE_KEYS = frozenset(
    {
        "state_version",
        "stage",
        "direction",
        "scope_sha256",
        "accepted_documents",
        "pending_paths",
        "review_paths",
        "candidate_sha256",
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
class SourceChangeInventory:
    """Bounded immutable inventory outside the closed eight-key continuation state."""

    files: tuple[SourceChange, ...]

    def __post_init__(self) -> None:
        _exact(self.files, tuple)
        if len(self.files) > 100 or any(type(item) is not SourceChange for item in self.files):
            raise _fail()
        paths = tuple(item.path.value for item in self.files)
        if paths != tuple(sorted(set(paths))):
            raise _fail()


def normalize_source_inventory(files: Sequence[Mapping[str, object]], /) -> SourceChangeInventory:
    """Project the existing PR files response onto the exact inputs we consume."""
    if len(files) > 100:
        raise _fail()
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
    return json.dumps(
        [
            {
                "path": item.path.value,
                "status": item.status,
                "previous_path": None if item.previous_path is None else item.previous_path.value,
                "rename_changed": item.rename_changed,
            }
            for item in inventory.files
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_source_inventory(raw: str | bytes, /) -> SourceChangeInventory:
    if type(raw) not in {str, bytes} or len(raw) > 4_000_000:
        raise _fail()
    try:
        parsed = json.loads(raw, object_pairs_hook=_ObjectPairs)
        _exact(parsed, list)
        if len(parsed) > 100:
            raise _fail()
        files = []
        for value in parsed:
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
        return SourceChangeInventory(tuple(files))
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
    accepted_documents: tuple[AcceptedDocument, ...]
    pending_paths: tuple[RepoPath, ...]
    review_paths: tuple[RepoPath, ...]
    candidate_sha256: ContentHash | None

    def __post_init__(self) -> None:
        _exact(self.state_version, int)
        _exact(self.stage, ContinuationStage)
        if self.state_version != STATE_VERSION:
            raise _fail()
        if self.direction is not None:
            _exact(self.direction, Direction)
        if self.scope_sha256 is not None:
            _exact(self.scope_sha256, ContentHash)
        _exact(self.accepted_documents, tuple)
        if any(type(item) is not AcceptedDocument for item in self.accepted_documents):
            raise _fail()
        accepted_paths = tuple(item.target_path for item in self.accepted_documents)
        if accepted_paths != tuple(sorted(accepted_paths, key=lambda item: item.value)):
            raise _fail()
        if len(accepted_paths) != len(set(accepted_paths)):
            raise _fail()
        pending = _exact_paths(self.pending_paths)
        review = _exact_paths(self.review_paths)
        if set(accepted_paths) & set(pending):
            raise _fail()
        if self.candidate_sha256 is not None:
            _exact(self.candidate_sha256, ContentHash)

        if self.stage is ContinuationStage.DIRECTION:
            if any(
                (
                    self.direction is not None,
                    self.scope_sha256 is not None,
                    bool(self.accepted_documents),
                    bool(pending),
                    bool(review),
                    self.candidate_sha256 is not None,
                )
            ):
                raise _fail()
        elif self.stage is ContinuationStage.TRANSLATION:
            if (
                self.direction is None
                or self.scope_sha256 is None
                or not pending
                or review
                or self.candidate_sha256 is not None
            ):
                raise _fail()
        elif (
            self.direction is None
            or self.scope_sha256 is None
            or pending
            or not review
            or self.candidate_sha256 is None
        ):
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


def _accepted_documents(value: object) -> tuple[AcceptedDocument, ...]:
    accepted: list[AcceptedDocument] = []
    for target_path, raw_markdown in _pairs(value):
        _exact(raw_markdown, str)
        accepted.append(AcceptedDocument(_path(target_path), cast(str, raw_markdown)))
    return tuple(sorted(accepted, key=lambda item: item.target_path.value))


def decode_state(raw: str | bytes, /) -> ContinuationState:
    """Decode state v2 while rejecting duplicate keys and all schema drift."""
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
            _accepted_documents(values["accepted_documents"]),
            _path_list(values["pending_paths"]),
            _path_list(values["review_paths"]),
            _optional_hash(values["candidate_sha256"]),
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
        "accepted_documents": {
            item.target_path.value: item.translated_markdown
            for item in state.accepted_documents
        },
        "pending_paths": [item.value for item in state.pending_paths],
        "review_paths": [item.value for item in state.review_paths],
        "candidate_sha256": (
            None if state.candidate_sha256 is None else state.candidate_sha256.value
        ),
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
    manifest: ScopeManifest, inventory: SourceChangeInventory, /
) -> ContentHash:
    """Bind a checkpoint's scope to the exact metadata and PR-file provenance."""
    canonical = json.dumps(
        {
            "scope_sha256": scope_sha256(manifest).value,
            "source_inventory": json.loads(encode_source_inventory(inventory)),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ContentHash(sha256(canonical.encode("utf-8")).hexdigest())


def validate_restored_documents(
    state: ContinuationState, restored_plans: tuple[RestoredPlan, ...], /
) -> None:
    """Bind every saved full-document path back to an authoritative source plan."""
    _exact(state, ContinuationState)
    _exact(restored_plans, tuple)
    if any(type(item) is not RestoredPlan for item in restored_plans):
        raise _fail()
    plans = {item.target_path: item for item in restored_plans}
    if len(plans) != len(restored_plans):
        raise _fail()
    referenced = (
        {item.target_path for item in state.accepted_documents}
        | set(state.pending_paths)
        | set(state.review_paths)
    )
    if not referenced.issubset(plans):
        raise _fail()
    try:
        for accepted in state.accepted_documents:
            plans[accepted.target_path]
            accepted.translated_markdown.encode("utf-8")
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise _fail() from None
