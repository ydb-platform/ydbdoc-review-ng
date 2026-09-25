"""Immutable scope construction and direction projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, cast

from ydbdoc_review_ng.anchors import markdown_anchors
from ydbdoc_review_ng.dependencies import (
    DependencyInputReason,
    DependencyLink,
    DependencyResolutionState,
    DependencySource,
    InvalidDependencyInput,
    RedirectCatalog,
    ResolvedDependency,
    resolve_redirect,
)
from ydbdoc_review_ng.direction import (
    Direction,
    DirectionPairVerdict,
    DirectionSelectionResult,
    DirectionSelectionState,
)
from ydbdoc_review_ng.domain import FilePair, Locale, RepoPath, SnapshotRef
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.links import closest_target_anchor
from ydbdoc_review_ng.locales import (
    ChangedFileKind,
    ChangedMarkdownFile,
    LocalePairInventory,
    LocaleRoots,
    PairKey,
    RenameContentState,
    paired_markdown_path,
)
from ydbdoc_review_ng.ports import SnapshotReader
from ydbdoc_review_ng.repository import ResolvedRepositorySnapshots

__all__ = [
    "DependencyWitness",
    "DirectionalPotentialScope",
    "FileOperation",
    "InitialPairDisposition",
    "InitialPairOutcome",
    "InvalidScopeInput",
    "PotentialScopeSet",
    "ScopeEntry",
    "ScopeError",
    "ScopeInputReason",
    "ScopeManifest",
    "ScopeMeasurement",
    "ScopeOrigin",
    "ScopePreflight",
    "ScopePreflightRequest",
    "ScopeSelection",
    "ScopeSelectionState",
    "build_potential_scopes",
    "freeze_scope_manifest",
]


def _invariant(type_name: str, field_name: str, expectation: str) -> InvariantViolation:
    return InvariantViolation(f"{type_name}.{field_name}: expected {expectation}")


def _exact(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    if type(value) is not expected:
        raise _invariant(type_name, field_name, f"exact {expected.__name__}")


def _optional(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    if value is not None and type(value) is not expected:
        raise _invariant(type_name, field_name, f"None or exact {expected.__name__}")


def _tuple_elements(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    _exact(value, tuple, type_name, field_name)
    for item in cast(tuple[object, ...], value):
        _exact(item, expected, type_name, field_name)


class ScopeOrigin(str, Enum):
    INITIAL = "initial"
    DEPENDENCY = "dependency"


class FileOperation(str, Enum):
    TRANSLATE = "translate"
    DELETE_TARGET = "delete_target"
    RENAME_TARGET = "rename_target"
    RENAME_TARGET_AND_TRANSLATE = "rename_target_and_translate"
    NOOP_TARGET_ABSENT = "noop_target_absent"
    NOOP_TARGET_ALREADY_RENAMED = "noop_target_already_renamed"
    SKIP_SOURCE_TOMBSTONE = "skip_source_tombstone"
    SKIP_TARGET_TOMBSTONE = "skip_target_tombstone"


class InitialPairDisposition(str, Enum):
    SELECTED = "selected"
    COMPLETE_PAIR = "complete_pair"


class ScopeSelectionState(str, Enum):
    SELECTED = "selected"
    NO_TRANSLATE = "no_translate"
    DIRECTION_UNDETERMINED = "direction_undetermined"


class ScopeInputReason(str, Enum):
    EMPTY_INITIAL_CHANGES = "empty_initial_changes"
    DUPLICATE_PAIR_KEY = "duplicate_pair_key"
    MIXED_ROOTS = "mixed_roots"
    MIXED_SCOPE_SNAPSHOT = "mixed_scope_snapshot"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    DIRECTION_NOT_POTENTIAL = "direction_not_potential"
    DIRECTION_RESULT_MISMATCH = "direction_result_mismatch"
    TARGET_PATH_COLLISION = "target_path_collision"
    AMBIGUOUS_RENAME_TARGET = "ambiguous_rename_target"
    INVALID_UTF8_SOURCE = "invalid_utf8_source"
    INVALID_PREFLIGHT_RESULT = "invalid_preflight_result"


class ScopeError(ValueError):
    """Base class for scope-boundary failures."""


class InvalidScopeInput(ScopeError):
    reason: ScopeInputReason
    direction: Direction | None
    path: RepoPath | None

    def __init__(
        self,
        reason: ScopeInputReason,
        direction: Direction | None,
        path: RepoPath | None,
        /,
    ) -> None:
        _exact(reason, ScopeInputReason, "InvalidScopeInput", "reason")
        _optional(direction, Direction, "InvalidScopeInput", "direction")
        _optional(path, RepoPath, "InvalidScopeInput", "path")
        self.reason = reason
        self.direction = direction
        self.path = path
        super().__init__(f"invalid_scope_input:{reason.value}")


@dataclass(frozen=True, slots=True)
class ScopeEntry:
    pair: FilePair
    source_content: bytes | None = field(repr=False)
    target_content: bytes | None = field(repr=False)
    origin: ScopeOrigin
    operation: FileOperation
    initial_keys: tuple[PairKey, ...]
    rename_from_target_path: RepoPath | None
    rename_from_target_content: bytes | None = field(repr=False)

    def __post_init__(self) -> None:
        name = "ScopeEntry"
        _exact(self.pair, FilePair, name, "pair")
        _optional(self.source_content, bytes, name, "source_content")
        _optional(self.target_content, bytes, name, "target_content")
        _exact(self.origin, ScopeOrigin, name, "origin")
        _exact(self.operation, FileOperation, name, "operation")
        _tuple_elements(self.initial_keys, PairKey, name, "initial_keys")
        _optional(self.rename_from_target_path, RepoPath, name, "rename_from_target_path")
        _optional(self.rename_from_target_content, bytes, name, "rename_from_target_content")
        key_values = tuple(key.relative_path.value for key in self.initial_keys)
        if not key_values or key_values != tuple(sorted(set(key_values))):
            raise _invariant(name, "initial_keys", "a non-empty unique key-sorted tuple")
        translate = self.operation is FileOperation.TRANSLATE
        rename_translate = self.operation is FileOperation.RENAME_TARGET_AND_TRANSLATE
        if self.origin is ScopeOrigin.DEPENDENCY and not translate:
            raise _invariant(name, "operation", "TRANSLATE for dependency origin")
        if translate:
            valid = (
                self.source_content is not None
                and self.rename_from_target_path is None
                and self.rename_from_target_content is None
            )
        elif self.operation is FileOperation.DELETE_TARGET:
            valid = (
                self.source_content is None
                and self.target_content is not None
                and self.rename_from_target_path is None
                and self.rename_from_target_content is None
            )
        elif self.operation is FileOperation.RENAME_TARGET:
            valid = (
                self.source_content is None
                and self.target_content is None
                and self.rename_from_target_path is not None
                and self.rename_from_target_content is not None
            )
        elif rename_translate:
            valid = (
                self.source_content is not None
                and self.target_content is None
                and self.rename_from_target_path is not None
                and self.rename_from_target_content is not None
            )
        elif self.operation is FileOperation.NOOP_TARGET_ALREADY_RENAMED:
            valid = (
                self.source_content is None
                and self.target_content is not None
                and self.rename_from_target_path is None
                and self.rename_from_target_content is None
            )
        elif self.operation is FileOperation.NOOP_TARGET_ABSENT:
            valid = (
                self.source_content is None
                and self.target_content is None
                and self.rename_from_target_path is None
                and self.rename_from_target_content is None
            )
        else:
            valid = (
                self.source_content is None
                and self.rename_from_target_path is None
                and self.rename_from_target_content is None
            )
        if not valid:
            raise _invariant(name, "operation", "the exact operation content shape")
        if self.origin is ScopeOrigin.INITIAL:
            own_keys = tuple(
                key
                for key in self.initial_keys
                if self.pair.source_path.value.endswith("/" + key.relative_path.value)
                and self.pair.target_path.value.endswith("/" + key.relative_path.value)
            )
            if len(own_keys) != 1:
                raise _invariant(name, "initial_keys", "the initial pair's own key")


@dataclass(frozen=True, slots=True)
class DependencyWitness:
    dependency_source_path: RepoPath
    referring_source_path: RepoPath

    def __post_init__(self) -> None:
        _exact(self.dependency_source_path, RepoPath, "DependencyWitness", "dependency_source_path")
        _exact(self.referring_source_path, RepoPath, "DependencyWitness", "referring_source_path")


@dataclass(frozen=True, slots=True)
class ScopeMeasurement:
    direction: Direction
    dependency_file_count: int
    source_character_count: int
    dependency_witnesses: tuple[DependencyWitness, ...]

    def __post_init__(self) -> None:
        name = "ScopeMeasurement"
        _exact(self.direction, Direction, name, "direction")
        _exact(self.dependency_file_count, int, name, "dependency_file_count")
        _exact(self.source_character_count, int, name, "source_character_count")
        _tuple_elements(self.dependency_witnesses, DependencyWitness, name, "dependency_witnesses")
        if self.dependency_file_count < 0:
            raise _invariant(name, "dependency_file_count", "a nonnegative integer")
        if self.source_character_count < 0:
            raise _invariant(name, "source_character_count", "a nonnegative integer")
        keys = tuple(
            (w.dependency_source_path.value, w.referring_source_path.value)
            for w in self.dependency_witnesses
        )
        if keys != tuple(sorted(set(keys))) or len({key[0] for key in keys}) != len(keys):
            raise _invariant(name, "dependency_witnesses", "canonical unique dependency witnesses")
        if len(keys) != self.dependency_file_count:
            raise _invariant(name, "dependency_witnesses", "one witness per dependency file")


@dataclass(frozen=True, slots=True)
class ScopePreflightRequest:
    scope_snapshot: SnapshotRef
    measurements: tuple[ScopeMeasurement, ...]

    def __post_init__(self) -> None:
        _exact(self.scope_snapshot, SnapshotRef, "ScopePreflightRequest", "scope_snapshot")
        _tuple_elements(
            self.measurements, ScopeMeasurement, "ScopePreflightRequest", "measurements"
        )
        directions = tuple(item.direction for item in self.measurements)
        expected = tuple(
            direction
            for direction in (Direction.RU_TO_EN, Direction.EN_TO_RU)
            if direction in directions
        )
        if directions != expected or len(directions) != len(set(directions)):
            raise _invariant(
                "ScopePreflightRequest", "measurements", "unique direction-canonical measurements"
            )


@dataclass(frozen=True, slots=True)
class DirectionalPotentialScope:
    direction: Direction
    scope_snapshot: SnapshotRef
    roots: LocaleRoots
    initial_pairs: tuple[LocalePairInventory, ...]
    entries: tuple[ScopeEntry, ...]
    dependencies: tuple[ResolvedDependency, ...]
    measurement: ScopeMeasurement

    def __post_init__(self) -> None:
        name = "DirectionalPotentialScope"
        _exact(self.direction, Direction, name, "direction")
        _exact(self.scope_snapshot, SnapshotRef, name, "scope_snapshot")
        _exact(self.roots, LocaleRoots, name, "roots")
        _tuple_elements(self.initial_pairs, LocalePairInventory, name, "initial_pairs")
        _tuple_elements(self.entries, ScopeEntry, name, "entries")
        _tuple_elements(self.dependencies, ResolvedDependency, name, "dependencies")
        _exact(self.measurement, ScopeMeasurement, name, "measurement")
        pair_keys = tuple(pair.key.relative_path.value for pair in self.initial_pairs)
        if pair_keys != tuple(sorted(set(pair_keys))):
            raise _invariant(name, "initial_pairs", "unique key-sorted pairs")
        if any(
            pair.roots != self.roots or pair.ru.snapshot != self.scope_snapshot
            for pair in self.initial_pairs
        ):
            raise _invariant(name, "initial_pairs", "one roots and scope snapshot")
        entry_keys = tuple(
            (e.pair.target_path.value, e.pair.source_path.value, e.operation.value, e.origin.value)
            for e in self.entries
        )
        if entry_keys != tuple(sorted(entry_keys)) or len(
            {e.pair.target_path for e in self.entries}
        ) != len(self.entries):
            raise _invariant(name, "entries", "canonical entries with unique target paths")
        dep_keys = tuple(_dependency_key(item) for item in self.dependencies)
        if dep_keys != tuple(sorted(set(dep_keys))):
            raise _invariant(name, "dependencies", "canonical unique dependencies")
        if self.measurement.direction is not self.direction:
            raise _invariant(name, "measurement", "matching direction")
        source_locale, target_locale = _locales(self.direction)
        if any(
            e.pair.source_locale is not source_locale or e.pair.target_locale is not target_locale
            for e in self.entries
        ):
            raise _invariant(name, "entries", "pairs matching direction")
        pair_key_objects = tuple(pair.key for pair in self.initial_pairs)
        initial_entries = tuple(e for e in self.entries if e.origin is ScopeOrigin.INITIAL)
        if tuple(e.initial_keys[0] for e in initial_entries) != pair_key_objects:
            raise _invariant(name, "entries", "one canonical initial entry per initial pair")
        if any(key not in pair_key_objects for entry in self.entries for key in entry.initial_keys):
            raise _invariant(name, "entries", "initial keys from initial pairs")
        dependency_entries = tuple(e for e in self.entries if e.origin is ScopeOrigin.DEPENDENCY)
        expected_characters = sum(
            len(e.source_content.decode("utf-8"))
            for e in self.entries
            if e.source_content is not None
            and e.operation in {FileOperation.TRANSLATE, FileOperation.RENAME_TARGET_AND_TRANSLATE}
        )
        if self.measurement.dependency_file_count != len(dependency_entries):
            raise _invariant(name, "measurement", "the dependency entry count")
        if self.measurement.source_character_count != expected_characters:
            raise _invariant(name, "measurement", "the source character count")


@dataclass(frozen=True, slots=True)
class PotentialScopeSet:
    scope_snapshot: SnapshotRef
    roots: LocaleRoots
    scopes: tuple[DirectionalPotentialScope, ...]
    preflight_request: ScopePreflightRequest | None

    def __post_init__(self) -> None:
        name = "PotentialScopeSet"
        _exact(self.scope_snapshot, SnapshotRef, name, "scope_snapshot")
        _exact(self.roots, LocaleRoots, name, "roots")
        _tuple_elements(self.scopes, DirectionalPotentialScope, name, "scopes")
        _optional(self.preflight_request, ScopePreflightRequest, name, "preflight_request")
        directions = tuple(scope.direction for scope in self.scopes)
        expected = tuple(
            direction
            for direction in (Direction.RU_TO_EN, Direction.EN_TO_RU)
            if direction in directions
        )
        if directions != expected or len(directions) != len(set(directions)):
            raise _invariant(name, "scopes", "unique direction-canonical scopes")
        if any(
            scope.scope_snapshot != self.scope_snapshot or scope.roots != self.roots
            for scope in self.scopes
        ):
            raise _invariant(name, "scopes", "matching snapshot and roots")
        if not self.scopes:
            if self.preflight_request is not None:
                raise _invariant(name, "preflight_request", "None for empty scopes")
        elif self.preflight_request != ScopePreflightRequest(
            self.scope_snapshot, tuple(scope.measurement for scope in self.scopes)
        ):
            raise _invariant(name, "preflight_request", "the exact scope measurement request")


@dataclass(frozen=True, slots=True)
class InitialPairOutcome:
    key: PairKey
    disposition: InitialPairDisposition
    entry: ScopeEntry | None

    def __post_init__(self) -> None:
        _exact(self.key, PairKey, "InitialPairOutcome", "key")
        _exact(self.disposition, InitialPairDisposition, "InitialPairOutcome", "disposition")
        _optional(self.entry, ScopeEntry, "InitialPairOutcome", "entry")
        if (self.disposition is InitialPairDisposition.SELECTED) != (self.entry is not None):
            raise _invariant("InitialPairOutcome", "entry", "an entry exactly for SELECTED")
        if self.entry is not None and (
            self.entry.origin is not ScopeOrigin.INITIAL or self.key not in self.entry.initial_keys
        ):
            raise _invariant("InitialPairOutcome", "entry", "the matching initial entry")


@dataclass(frozen=True, slots=True)
class ScopeManifest:
    direction: Direction
    scope_snapshot: SnapshotRef
    roots: LocaleRoots
    initial_outcomes: tuple[InitialPairOutcome, ...]
    entries: tuple[ScopeEntry, ...]
    dependency_file_count: int
    source_character_count: int

    def __post_init__(self) -> None:
        name = "ScopeManifest"
        _exact(self.direction, Direction, name, "direction")
        _exact(self.scope_snapshot, SnapshotRef, name, "scope_snapshot")
        _exact(self.roots, LocaleRoots, name, "roots")
        _tuple_elements(self.initial_outcomes, InitialPairOutcome, name, "initial_outcomes")
        _tuple_elements(self.entries, ScopeEntry, name, "entries")
        _exact(self.dependency_file_count, int, name, "dependency_file_count")
        _exact(self.source_character_count, int, name, "source_character_count")
        keys = tuple(outcome.key.relative_path.value for outcome in self.initial_outcomes)
        if keys != tuple(sorted(set(keys))):
            raise _invariant(name, "initial_outcomes", "unique key-sorted outcomes")
        entry_keys = tuple(_entry_key(entry) for entry in self.entries)
        if entry_keys != tuple(sorted(entry_keys)) or len(
            {entry.pair.target_path for entry in self.entries}
        ) != len(self.entries):
            raise _invariant(name, "entries", "canonical entries with unique target paths")
        if type(self.dependency_file_count) is not int or self.dependency_file_count < 0:
            raise _invariant(name, "dependency_file_count", "a nonnegative exact int")
        if type(self.source_character_count) is not int or self.source_character_count < 0:
            raise _invariant(name, "source_character_count", "a nonnegative exact int")
        if (
            sum(entry.origin is ScopeOrigin.DEPENDENCY for entry in self.entries)
            != self.dependency_file_count
        ):
            raise _invariant(name, "dependency_file_count", "the retained dependency entry count")
        expected_characters = sum(
            len(entry.source_content.decode("utf-8"))
            for entry in self.entries
            if entry.source_content is not None
            and entry.operation
            in {FileOperation.TRANSLATE, FileOperation.RENAME_TARGET_AND_TRANSLATE}
        )
        if self.source_character_count != expected_characters:
            raise _invariant(name, "source_character_count", "the retained source character count")
        if any(
            outcome.entry is not None and outcome.entry not in self.entries
            for outcome in self.initial_outcomes
        ):
            raise _invariant(name, "initial_outcomes", "entries retained by the manifest")


@dataclass(frozen=True, slots=True)
class ScopeSelection:
    state: ScopeSelectionState
    manifest: ScopeManifest | None

    def __post_init__(self) -> None:
        _exact(self.state, ScopeSelectionState, "ScopeSelection", "state")
        _optional(self.manifest, ScopeManifest, "ScopeSelection", "manifest")
        if (self.state is ScopeSelectionState.SELECTED) != (self.manifest is not None):
            raise _invariant("ScopeSelection", "manifest", "a manifest exactly for SELECTED")


class ScopePreflight(Protocol):
    def check(self, request: ScopePreflightRequest, /) -> None: ...


def _locales(direction: Direction) -> tuple[Locale, Locale]:
    if direction is Direction.RU_TO_EN:
        return Locale.RU, Locale.EN
    return Locale.EN, Locale.RU


def _entry_key(entry: ScopeEntry) -> tuple[str, str, str, str]:
    return (
        entry.pair.target_path.value,
        entry.pair.source_path.value,
        entry.operation.value,
        entry.origin.value,
    )


def _dependency_key(item: ResolvedDependency) -> tuple[str, str, str, str, str, str]:
    return (
        item.link.source_path.value,
        item.link.destination_source_path.value,
        item.link.fragment or "",
        item.source_path.value,
        item.target_path.value,
        item.state.value,
    )


def _scope_error(
    reason: ScopeInputReason, direction: Direction | None = None, path: RepoPath | None = None
) -> InvalidScopeInput:
    return InvalidScopeInput(reason, direction, path)


def _change_for(inventory: LocalePairInventory, locale: Locale) -> ChangedMarkdownFile | None:
    return next((change for change in inventory.changes if change.locale is locale), None)


def _pair_for(direction: Direction, source_path: RepoPath, target_path: RepoPath) -> FilePair:
    source_locale, target_locale = _locales(direction)
    return FilePair(source_locale, target_locale, source_path, target_path)


def _is_historical_changelog(path: RepoPath) -> bool:
    return path.value.rsplit("/", 1)[-1] in {
        "changelog-enterprise.md",
        "changelog-server.md",
    }


def _target_metadata_state(
    inventory: LocalePairInventory,
    target_locale: Locale,
    source_change: ChangedMarkdownFile,
) -> str:
    target_change = _change_for(inventory, target_locale)
    if target_change is None:
        return "ABS"
    if target_change.kind is ChangedFileKind.ADDED:
        return "ADD"
    if target_change.kind is ChangedFileKind.MODIFIED:
        return "MOD"
    if (
        target_change.kind is ChangedFileKind.RENAMED
        and target_change.previous_key == source_change.previous_key
        and target_change.key == source_change.key
    ):
        return "PAIR"
    return "OTHER"


def _decode_count(content: bytes, direction: Direction, path: RepoPath) -> int:
    try:
        return len(content.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        raise _scope_error(ScopeInputReason.INVALID_UTF8_SOURCE, direction, path) from None


def _directions(inventories: tuple[LocalePairInventory, ...]) -> tuple[Direction, ...]:
    locales = {change.locale for inventory in inventories for change in inventory.changes}
    if locales == {Locale.RU}:
        return (Direction.RU_TO_EN,)
    if locales == {Locale.EN}:
        return (Direction.EN_TO_RU,)
    return (Direction.RU_TO_EN, Direction.EN_TO_RU)


def _validate_build_inputs(
    reader: SnapshotReader,
    dependency_source: DependencySource,
    preflight: ScopePreflight,
    snapshots: ResolvedRepositorySnapshots,
    inventories: tuple[LocalePairInventory, ...],
    redirects: RedirectCatalog,
) -> tuple[LocalePairInventory, ...]:
    if not callable(getattr(reader, "read_bytes", None)):
        raise _invariant("build_potential_scopes", "reader", "a SnapshotReader capability")
    if not callable(getattr(dependency_source, "links", None)):
        raise _invariant(
            "build_potential_scopes", "dependency_source", "a DependencySource capability"
        )
    if not callable(getattr(preflight, "check", None)):
        raise _invariant("build_potential_scopes", "preflight", "a ScopePreflight capability")
    _exact(snapshots, ResolvedRepositorySnapshots, "build_potential_scopes", "snapshots")
    _tuple_elements(inventories, LocalePairInventory, "build_potential_scopes", "inventories")
    _exact(redirects, RedirectCatalog, "build_potential_scopes", "redirects")
    if redirects.snapshot != snapshots.scope_snapshot:
        raise _scope_error(ScopeInputReason.SNAPSHOT_MISMATCH)
    if not inventories:
        return ()
    empty = sorted(
        (item for item in inventories if not item.changes), key=lambda item: item.ru.path.value
    )
    if empty:
        raise _scope_error(ScopeInputReason.EMPTY_INITIAL_CHANGES, None, empty[0].ru.path)
    ordered = tuple(sorted(inventories, key=lambda item: item.key.relative_path.value))
    repeated = sorted(
        {
            item.key.relative_path.value
            for item in ordered
            if sum(other.key == item.key for other in ordered) > 1
        }
    )
    if repeated:
        offender = next(item for item in ordered if item.key.relative_path.value == repeated[0])
        raise _scope_error(ScopeInputReason.DUPLICATE_PAIR_KEY, None, offender.ru.path)
    wrong_roots = sorted(
        (item for item in ordered if item.roots != redirects.roots),
        key=lambda item: item.ru.path.value,
    )
    if wrong_roots:
        raise _scope_error(ScopeInputReason.MIXED_ROOTS, None, wrong_roots[0].ru.path)
    snapshots_seen = {item.ru.snapshot for item in ordered}
    if len(snapshots_seen) > 1:
        first = min(ordered, key=lambda item: item.ru.path.value)
        raise _scope_error(ScopeInputReason.MIXED_SCOPE_SNAPSHOT, None, first.ru.path)
    if ordered[0].ru.snapshot != snapshots.scope_snapshot:
        raise _scope_error(ScopeInputReason.SNAPSHOT_MISMATCH, None, ordered[0].ru.path)
    return ordered


def build_potential_scopes(
    reader: SnapshotReader,
    dependency_source: DependencySource,
    preflight: ScopePreflight,
    snapshots: ResolvedRepositorySnapshots,
    inventories: tuple[LocalePairInventory, ...],
    redirects: RedirectCatalog,
    /,
) -> PotentialScopeSet:
    canonical = _validate_build_inputs(
        reader, dependency_source, preflight, snapshots, inventories, redirects
    )
    if not canonical:
        return PotentialScopeSet(snapshots.scope_snapshot, redirects.roots, (), None)

    read_cache: dict[RepoPath, bytes | None] = {
        side.path: side.content for inventory in canonical for side in (inventory.ru, inventory.en)
    }

    def read(path: RepoPath) -> bytes | None:
        if path not in read_cache:
            value = reader.read_bytes(snapshots.scope_snapshot, path)
            if value is not None and type(value) is not bytes:
                raise _invariant("build_potential_scopes", "reader", "bytes or None result")
            read_cache[path] = value
        return read_cache[path]

    scopes = tuple(
        _build_direction(
            direction,
            dependency_source,
            snapshots.scope_snapshot,
            canonical,
            redirects,
            read,
        )
        for direction in _directions(canonical)
    )
    request = ScopePreflightRequest(
        snapshots.scope_snapshot, tuple(scope.measurement for scope in scopes)
    )
    check = cast(Callable[[ScopePreflightRequest], object], preflight.check)
    result = check(request)
    if result is not None:
        raise _scope_error(ScopeInputReason.INVALID_PREFLIGHT_RESULT)
    return PotentialScopeSet(snapshots.scope_snapshot, redirects.roots, scopes, request)


def _build_direction(
    direction: Direction,
    dependency_source: DependencySource,
    snapshot: SnapshotRef,
    inventories: tuple[LocalePairInventory, ...],
    redirects: RedirectCatalog,
    read: Callable[[RepoPath], bytes | None],
) -> DirectionalPotentialScope:
    source_locale, target_locale = _locales(direction)
    redirect_from = {entry.from_path for entry in redirects.entries}
    entries: list[ScopeEntry] = []

    for inventory in inventories:
        source_file = inventory.ru if source_locale is Locale.RU else inventory.en
        target_file = inventory.en if target_locale is Locale.EN else inventory.ru
        source_path = source_file.path
        target_path = target_file.path
        source_change = _change_for(inventory, source_locale)
        operation: FileOperation
        source_content: bytes | None = None
        target_content: bytes | None = target_file.content
        rename_path: RepoPath | None = None
        rename_content: bytes | None = None

        if source_path in redirect_from:
            operation = FileOperation.SKIP_SOURCE_TOMBSTONE
        elif target_path in redirect_from:
            operation = FileOperation.SKIP_TARGET_TOMBSTONE
        elif source_file.content is None:
            operation = (
                FileOperation.DELETE_TARGET
                if target_file.content is not None
                else FileOperation.NOOP_TARGET_ABSENT
            )
        elif source_change is not None and source_change.kind is ChangedFileKind.RENAMED:
            assert source_change.previous_key is not None
            old_ru = RepoPath(
                f"{inventory.roots.ru.value}/{source_change.previous_key.relative_path.value}"
            )
            old_en = RepoPath(
                f"{inventory.roots.en.value}/{source_change.previous_key.relative_path.value}"
            )
            old_values = {Locale.RU: read(old_ru), Locale.EN: read(old_en)}
            old_target_path = old_en if target_locale is Locale.EN else old_ru
            old_exists = old_values[target_locale] is not None
            new_exists = target_file.content is not None
            metadata = _target_metadata_state(inventory, target_locale, source_change)
            rename_state = source_change.rename_content_state
            if old_exists and new_exists:
                raise _scope_error(ScopeInputReason.AMBIGUOUS_RENAME_TARGET, direction, target_path)
            if not old_exists and new_exists:
                if metadata == "PAIR":
                    operation = (
                        FileOperation.NOOP_TARGET_ALREADY_RENAMED
                        if rename_state is RenameContentState.UNCHANGED
                        else FileOperation.TRANSLATE
                    )
                else:
                    raise _scope_error(
                        ScopeInputReason.TARGET_PATH_COLLISION, direction, target_path
                    )
            elif old_exists and not new_exists:
                if metadata != "ABS":
                    raise _scope_error(
                        ScopeInputReason.TARGET_PATH_COLLISION, direction, target_path
                    )
                operation = (
                    FileOperation.RENAME_TARGET
                    if rename_state is RenameContentState.UNCHANGED
                    else FileOperation.RENAME_TARGET_AND_TRANSLATE
                )
            else:
                operation = FileOperation.TRANSLATE
            if operation in {
                FileOperation.RENAME_TARGET,
                FileOperation.RENAME_TARGET_AND_TRANSLATE,
            }:
                rename_path = old_target_path
                rename_content = old_values[target_locale]
                target_content = None
            if operation in {FileOperation.TRANSLATE, FileOperation.RENAME_TARGET_AND_TRANSLATE}:
                source_content = source_file.content
            elif operation is FileOperation.NOOP_TARGET_ALREADY_RENAMED:
                target_content = target_file.content
        else:
            operation = FileOperation.TRANSLATE
            source_content = source_file.content

        entry = ScopeEntry(
            _pair_for(direction, source_path, target_path),
            source_content,
            target_content,
            ScopeOrigin.INITIAL,
            operation,
            (inventory.key,),
            rename_path,
            rename_content,
        )
        entries.append(entry)

    _ensure_unique_targets(entries, direction)
    initial_by_source = {entry.pair.source_path: entry for entry in entries}
    queue = {
        entry.pair.source_path
        for entry in entries
        if entry.operation in {FileOperation.TRANSLATE, FileOperation.RENAME_TARGET_AND_TRANSLATE}
    }
    source_bytes = {
        entry.pair.source_path: entry.source_content
        for entry in entries
        if entry.source_content is not None
    }
    scanned: set[RepoPath] = set()
    resolved: set[ResolvedDependency] = set()
    missing_edges: set[tuple[RepoPath, RepoPath]] = set()
    dependency_paths: set[RepoPath] = set()

    while queue - scanned:
        current = min(queue - scanned, key=lambda path: path.value)
        scanned.add(current)
        current_content = source_bytes[current]
        links = dependency_source.links(snapshot, current, current_content)
        if type(links) is not tuple or any(type(link) is not DependencyLink for link in links):
            raise InvalidDependencyInput(DependencyInputReason.INVALID_SCANNER_RESULT, current)
        canonical_links = tuple(
            sorted(
                set(links),
                key=lambda link: (
                    link.source_path.value,
                    link.destination_source_path.value,
                    link.fragment or "",
                ),
            )
        )
        mismatches = sorted(
            (link.source_path for link in canonical_links if link.source_path != current),
            key=lambda path: path.value,
        )
        if mismatches:
            raise InvalidDependencyInput(DependencyInputReason.SOURCE_PATH_MISMATCH, mismatches[0])
        non_markdown = sorted(
            (
                link.destination_source_path
                for link in canonical_links
                if not link.destination_source_path.value.rsplit("/", 1)[-1].endswith(".md")
            ),
            key=lambda path: path.value,
        )
        if non_markdown:
            raise InvalidDependencyInput(
                DependencyInputReason.NON_MARKDOWN_DESTINATION, non_markdown[0]
            )
        source_root = redirects.roots.ru if source_locale is Locale.RU else redirects.roots.en
        target_root = redirects.roots.en if target_locale is Locale.EN else redirects.roots.ru
        mixed = sorted(
            (
                link.destination_source_path
                for link in canonical_links
                if link.destination_source_path.value.startswith(target_root.value + "/")
            ),
            key=lambda path: path.value,
        )
        if mixed:
            raise InvalidDependencyInput(DependencyInputReason.MIXED_LOCALE, mixed[0])
        outside = sorted(
            (
                link.destination_source_path
                for link in canonical_links
                if not link.destination_source_path.value.startswith(source_root.value + "/")
            ),
            key=lambda path: path.value,
        )
        if outside:
            raise InvalidDependencyInput(
                DependencyInputReason.PATH_OUTSIDE_SOURCE_LOCALE, outside[0]
            )
        for link in canonical_links:
            destination = link.destination_source_path
            terminal_source = resolve_redirect(redirects, source_locale, destination)
            target = paired_markdown_path(redirects.roots, terminal_source)
            dep_source_content = read(terminal_source)
            if dep_source_content is None:
                terminal_target = target
                dependency_state = DependencyResolutionState.SOURCE_MISSING
            else:
                terminal_target = resolve_redirect(redirects, target_locale, target)
                target_content = read(terminal_target)
                if target_content is not None:
                    missing_anchor = (
                        link.fragment is not None
                        and terminal_source.value.rsplit("/", 1)[-1] == "glossary.md"
                        and link.fragment in markdown_anchors(dep_source_content)
                        and link.fragment not in markdown_anchors(target_content)
                        and closest_target_anchor(
                            link.fragment, markdown_anchors(target_content)
                        )
                        is None
                    )
                    if missing_anchor:
                        dependency_state = (
                            DependencyResolutionState.TARGET_MISSING_ANCHOR_SOURCE_EXISTS
                        )
                        missing_edges.add((current, terminal_source))
                        source_bytes[terminal_source] = dep_source_content
                        queue.add(terminal_source)
                        if terminal_source not in initial_by_source:
                            dependency_paths.add(terminal_source)
                    else:
                        dependency_state = (
                            DependencyResolutionState.TARGET_REDIRECT_EXISTS
                            if terminal_target != target
                            else DependencyResolutionState.TARGET_EXISTS
                        )
                else:
                    dependency_state = DependencyResolutionState.TARGET_MISSING_SOURCE_EXISTS
                    missing_edges.add((current, terminal_source))
                    if not _is_historical_changelog(current):
                        source_bytes[terminal_source] = dep_source_content
                        queue.add(terminal_source)
                        if terminal_source not in initial_by_source:
                            dependency_paths.add(terminal_source)
            resolved.add(
                ResolvedDependency(link, terminal_source, terminal_target, dependency_state)
            )

    seed_roots = {
        entry.pair.source_path: set(entry.initial_keys)
        for entry in entries
        if entry.operation in {FileOperation.TRANSLATE, FileOperation.RENAME_TARGET_AND_TRANSLATE}
    }
    reaching = {path: set(keys) for path, keys in seed_roots.items()}
    changed = True
    while changed:
        changed = False
        for parent, child in sorted(missing_edges, key=lambda edge: (edge[0].value, edge[1].value)):
            before = len(reaching.get(child, set()))
            reaching.setdefault(child, set()).update(reaching.get(parent, set()))
            changed |= len(reaching[child]) != before

    for path in sorted(dependency_paths, key=lambda item: item.value):
        keys = tuple(sorted(reaching[path], key=lambda key: key.relative_path.value))
        target = resolve_redirect(
            redirects, target_locale, paired_markdown_path(redirects.roots, path)
        )
        entries.append(
            ScopeEntry(
                _pair_for(direction, path, target),
                source_bytes[path],
                read(target),
                ScopeOrigin.DEPENDENCY,
                FileOperation.TRANSLATE,
                keys,
                None,
                None,
            )
        )

    canonical_entries = tuple(sorted(entries, key=_entry_key))
    character_count = sum(
        _decode_count(entry.source_content, direction, entry.pair.source_path)
        for entry in canonical_entries
        if entry.operation in {FileOperation.TRANSLATE, FileOperation.RENAME_TARGET_AND_TRANSLATE}
        and entry.source_content is not None
    )
    _ensure_unique_targets(entries, direction)
    witnesses = tuple(
        DependencyWitness(
            path,
            min(
                (
                    parent
                    for parent, child in missing_edges
                    if child == path and reaching.get(parent)
                ),
                key=lambda item: item.value,
            ),
        )
        for path in sorted(dependency_paths, key=lambda item: item.value)
    )
    measurement = ScopeMeasurement(direction, len(dependency_paths), character_count, witnesses)
    return DirectionalPotentialScope(
        direction,
        snapshot,
        redirects.roots,
        inventories,
        canonical_entries,
        tuple(sorted(resolved, key=_dependency_key)),
        measurement,
    )


def _ensure_unique_targets(entries: list[ScopeEntry], direction: Direction) -> None:
    counts: dict[RepoPath, int] = {}
    for entry in entries:
        counts[entry.pair.target_path] = counts.get(entry.pair.target_path, 0) + 1
    collisions = sorted(
        (path for path, count in counts.items() if count > 1), key=lambda path: path.value
    )
    if collisions:
        raise _scope_error(ScopeInputReason.TARGET_PATH_COLLISION, direction, collisions[0])


def freeze_scope_manifest(
    potential: PotentialScopeSet,
    direction_result: DirectionSelectionResult,
    /,
) -> ScopeSelection:
    _exact(potential, PotentialScopeSet, "freeze_scope_manifest", "potential")
    _exact(direction_result, DirectionSelectionResult, "freeze_scope_manifest", "direction_result")
    reference_pairs = potential.scopes[0].initial_pairs if potential.scopes else ()
    decisions = direction_result.decisions
    if any(
        len(scope.initial_pairs) != len(reference_pairs)
        or any(
            pair is not reference
            for pair, reference in zip(scope.initial_pairs, reference_pairs, strict=True)
        )
        for scope in potential.scopes
    ):
        raise _scope_error(ScopeInputReason.DIRECTION_RESULT_MISMATCH, direction_result.direction)
    if tuple(decision.pair.key for decision in decisions) != tuple(
        pair.key for pair in reference_pairs
    ):
        raise _scope_error(ScopeInputReason.DIRECTION_RESULT_MISMATCH, direction_result.direction)
    if any(
        decision.pair is not pair for decision, pair in zip(decisions, reference_pairs, strict=True)
    ):
        raise _scope_error(ScopeInputReason.DIRECTION_RESULT_MISMATCH, direction_result.direction)
    if not potential.scopes:
        if direction_result.state is not DirectionSelectionState.NO_TRANSLATE or decisions:
            raise _scope_error(
                ScopeInputReason.DIRECTION_RESULT_MISMATCH, direction_result.direction
            )
        return ScopeSelection(ScopeSelectionState.NO_TRANSLATE, None)
    if direction_result.state is DirectionSelectionState.NO_TRANSLATE:
        if any(
            decision.verdict is not DirectionPairVerdict.COMPLETE_PAIR for decision in decisions
        ):
            raise _scope_error(
                ScopeInputReason.DIRECTION_RESULT_MISMATCH, direction_result.direction
            )
        return ScopeSelection(ScopeSelectionState.NO_TRANSLATE, None)
    if direction_result.state is DirectionSelectionState.DIRECTION_UNDETERMINED:
        return ScopeSelection(ScopeSelectionState.DIRECTION_UNDETERMINED, None)
    selected_scope = next(
        (scope for scope in potential.scopes if scope.direction is direction_result.direction), None
    )
    if selected_scope is None:
        raise _scope_error(ScopeInputReason.DIRECTION_NOT_POTENTIAL, direction_result.direction)
    selected_keys = {
        decision.pair.key
        for decision in decisions
        if decision.verdict is not DirectionPairVerdict.COMPLETE_PAIR
    }
    initial_entries = {
        entry.initial_keys[0]: entry
        for entry in selected_scope.entries
        if entry.origin is ScopeOrigin.INITIAL
    }
    outcomes = tuple(
        InitialPairOutcome(
            decision.pair.key,
            InitialPairDisposition.COMPLETE_PAIR
            if decision.verdict is DirectionPairVerdict.COMPLETE_PAIR
            else InitialPairDisposition.SELECTED,
            None
            if decision.verdict is DirectionPairVerdict.COMPLETE_PAIR
            else initial_entries[decision.pair.key],
        )
        for decision in decisions
    )
    retained = tuple(
        entry
        for entry in selected_scope.entries
        if (entry.origin is ScopeOrigin.INITIAL and entry.initial_keys[0] in selected_keys)
        or (
            entry.origin is ScopeOrigin.DEPENDENCY
            and any(key in selected_keys for key in entry.initial_keys)
        )
    )
    dependency_count = sum(entry.origin is ScopeOrigin.DEPENDENCY for entry in retained)
    characters = sum(
        _decode_count(entry.source_content, selected_scope.direction, entry.pair.source_path)
        for entry in retained
        if entry.operation in {FileOperation.TRANSLATE, FileOperation.RENAME_TARGET_AND_TRANSLATE}
        and entry.source_content is not None
    )
    manifest = ScopeManifest(
        selected_scope.direction,
        selected_scope.scope_snapshot,
        selected_scope.roots,
        outcomes,
        retained,
        dependency_count,
        characters,
    )
    return ScopeSelection(ScopeSelectionState.SELECTED, manifest)
