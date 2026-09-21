"""Direction-neutral locale path mapping and immutable pair discovery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import combinations

from ydbdoc_review_ng.domain import Locale, RepoPath, SnapshotRef
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.ports import SnapshotReader
from ydbdoc_review_ng.repository import ResolvedRepositorySnapshots

__all__ = (
    "ChangedFileKind",
    "ChangedFileMetadata",
    "ChangedMarkdownFile",
    "ConflictingChangedFileMetadata",
    "InvalidChangeReason",
    "InvalidChangedFileMetadata",
    "LocalePairInventory",
    "LocalePathError",
    "LocaleRoots",
    "LocalizedMarkdownPath",
    "MetadataConflictReason",
    "NonMarkdownPath",
    "PairDiscoveryError",
    "PairFileState",
    "PairKey",
    "PathOutsideLocaleRoots",
    "RenameContentState",
    "SnapshotLocaleFile",
    "classify_changed_file",
    "discover_changed_pairs",
    "is_markdown_path",
    "locate_markdown_path",
    "paired_markdown_path",
)


def _invariant(type_name: str, field_name: str, expectation: str) -> InvariantViolation:
    return InvariantViolation(f"{type_name}.{field_name}: expected {expectation}")


def _exact(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    if type(value) is not expected:
        raise _invariant(type_name, field_name, f"exact {expected.__name__}")


def _optional_exact(
    value: object, expected: type[object], type_name: str, field_name: str
) -> None:
    if value is not None and type(value) is not expected:
        raise _invariant(type_name, field_name, f"None or exact {expected.__name__}")


def _components(path: RepoPath) -> tuple[str, ...]:
    return tuple(path.value.split("/"))


def _is_prefix(prefix: RepoPath, path: RepoPath) -> bool:
    prefix_parts = _components(prefix)
    path_parts = _components(path)
    return len(path_parts) > len(prefix_parts) and path_parts[: len(prefix_parts)] == prefix_parts


def _join(root: RepoPath, key: PairKey) -> RepoPath:
    return RepoPath(f"{root.value}/{key.relative_path.value}")


@dataclass(frozen=True, slots=True)
class LocaleRoots:
    ru: RepoPath
    en: RepoPath

    def __post_init__(self) -> None:
        _exact(self.ru, RepoPath, "LocaleRoots", "ru")
        _exact(self.en, RepoPath, "LocaleRoots", "en")
        if self.ru == self.en:
            raise _invariant("LocaleRoots", "en", "a root different from ru")
        ru_parts = _components(self.ru)
        en_parts = _components(self.en)
        shared = min(len(ru_parts), len(en_parts))
        if ru_parts[:shared] == en_parts[:shared]:
            raise _invariant("LocaleRoots", "en", "non-nested locale roots")


@dataclass(frozen=True, slots=True)
class PairKey:
    relative_path: RepoPath

    def __post_init__(self) -> None:
        _exact(self.relative_path, RepoPath, "PairKey", "relative_path")
        if not self.relative_path.value.split("/")[-1].endswith(".md"):
            raise _invariant("PairKey", "relative_path", "a lowercase .md final component")


def _locate_components(roots: LocaleRoots, path: RepoPath) -> tuple[Locale, PairKey] | None:
    for locale, root in ((Locale.RU, roots.ru), (Locale.EN, roots.en)):
        if _is_prefix(root, path):
            suffix = "/".join(_components(path)[len(_components(root)) :])
            return locale, PairKey(RepoPath(suffix))
    return None


@dataclass(frozen=True, slots=True)
class LocalizedMarkdownPath:
    roots: LocaleRoots
    locale: Locale
    path: RepoPath
    key: PairKey

    def __post_init__(self) -> None:
        _exact(self.roots, LocaleRoots, "LocalizedMarkdownPath", "roots")
        _exact(self.locale, Locale, "LocalizedMarkdownPath", "locale")
        _exact(self.path, RepoPath, "LocalizedMarkdownPath", "path")
        _exact(self.key, PairKey, "LocalizedMarkdownPath", "key")
        located = _locate_components(self.roots, self.path)
        if located is None or located != (self.locale, self.key):
            raise _invariant(
                "LocalizedMarkdownPath", "path", "membership consistent with locale and key"
            )


class LocalePathError(ValueError):
    """Base class for locale path classification errors."""


class NonMarkdownPath(LocalePathError):
    path: RepoPath

    __slots__ = ("path",)

    def __init__(self, path: RepoPath, /) -> None:
        _exact(path, RepoPath, "NonMarkdownPath", "path")
        self.path = path
        super().__init__("non_markdown_path")


class PathOutsideLocaleRoots(LocalePathError):
    path: RepoPath

    __slots__ = ("path",)

    def __init__(self, path: RepoPath, /) -> None:
        _exact(path, RepoPath, "PathOutsideLocaleRoots", "path")
        self.path = path
        super().__init__("path_outside_locale_roots")


def is_markdown_path(path: RepoPath, /) -> bool:
    _exact(path, RepoPath, "is_markdown_path", "path")
    return path.value.split("/")[-1].endswith(".md")


def locate_markdown_path(roots: LocaleRoots, path: RepoPath, /) -> LocalizedMarkdownPath:
    _exact(roots, LocaleRoots, "locate_markdown_path", "roots")
    _exact(path, RepoPath, "locate_markdown_path", "path")
    if not is_markdown_path(path):
        raise NonMarkdownPath(path)
    located = _locate_components(roots, path)
    if located is None:
        raise PathOutsideLocaleRoots(path)
    locale, key = located
    return LocalizedMarkdownPath(roots, locale, path, key)


def paired_markdown_path(roots: LocaleRoots, path: RepoPath, /) -> RepoPath:
    localized = locate_markdown_path(roots, path)
    target_root = roots.en if localized.locale is Locale.RU else roots.ru
    return _join(target_root, localized.key)


class ChangedFileKind(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class RenameContentState(str, Enum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"


def _validate_change_shape(
    kind: ChangedFileKind,
    old_path: RepoPath | None,
    new_path: RepoPath | None,
    rename_content_state: RenameContentState | None,
    type_name: str,
) -> None:
    if kind is ChangedFileKind.ADDED:
        valid = old_path is None and new_path is not None and rename_content_state is None
    elif kind is ChangedFileKind.MODIFIED:
        valid = (
            old_path is not None
            and new_path is not None
            and old_path == new_path
            and rename_content_state is None
        )
    elif kind is ChangedFileKind.DELETED:
        valid = old_path is not None and new_path is None and rename_content_state is None
    else:
        valid = (
            old_path is not None
            and new_path is not None
            and old_path != new_path
            and rename_content_state is not None
        )
    if not valid:
        raise _invariant(type_name, "kind", "the exact path and rename-state shape for kind")


@dataclass(frozen=True, slots=True)
class ChangedFileMetadata:
    kind: ChangedFileKind
    old_path: RepoPath | None
    new_path: RepoPath | None
    rename_content_state: RenameContentState | None

    def __post_init__(self) -> None:
        _exact(self.kind, ChangedFileKind, "ChangedFileMetadata", "kind")
        _optional_exact(self.old_path, RepoPath, "ChangedFileMetadata", "old_path")
        _optional_exact(self.new_path, RepoPath, "ChangedFileMetadata", "new_path")
        _optional_exact(
            self.rename_content_state,
            RenameContentState,
            "ChangedFileMetadata",
            "rename_content_state",
        )
        _validate_change_shape(
            self.kind,
            self.old_path,
            self.new_path,
            self.rename_content_state,
            "ChangedFileMetadata",
        )


@dataclass(frozen=True, slots=True)
class ChangedMarkdownFile:
    roots: LocaleRoots
    kind: ChangedFileKind
    locale: Locale
    key: PairKey
    old_path: RepoPath | None
    new_path: RepoPath | None
    previous_key: PairKey | None
    rename_content_state: RenameContentState | None

    def __post_init__(self) -> None:
        type_name = "ChangedMarkdownFile"
        _exact(self.roots, LocaleRoots, type_name, "roots")
        _exact(self.kind, ChangedFileKind, type_name, "kind")
        _exact(self.locale, Locale, type_name, "locale")
        _exact(self.key, PairKey, type_name, "key")
        _optional_exact(self.old_path, RepoPath, type_name, "old_path")
        _optional_exact(self.new_path, RepoPath, type_name, "new_path")
        _optional_exact(self.previous_key, PairKey, type_name, "previous_key")
        _optional_exact(
            self.rename_content_state, RenameContentState, type_name, "rename_content_state"
        )
        _validate_change_shape(
            self.kind,
            self.old_path,
            self.new_path,
            self.rename_content_state,
            type_name,
        )
        present = tuple(path for path in (self.old_path, self.new_path) if path is not None)
        located = tuple(_locate_components(self.roots, path) for path in present)
        if any(item is None for item in located):
            raise _invariant(type_name, "old_path", "Markdown paths inside locale roots")
        typed_located = tuple(item for item in located if item is not None)
        if any(locale is not self.locale for locale, _ in typed_located):
            raise _invariant(type_name, "locale", "the locale of every present path")
        if self.kind is ChangedFileKind.RENAMED:
            old_located, new_located = typed_located
            if new_located[1] != self.key or old_located[1] != self.previous_key:
                raise _invariant(type_name, "key", "new key and previous old key")
            if self.previous_key == self.key:
                raise _invariant(type_name, "previous_key", "a key different from key")
        else:
            expected = typed_located[-1][1] if self.new_path is not None else typed_located[0][1]
            if self.key != expected or self.previous_key is not None:
                raise _invariant(type_name, "key", "the current path key without previous_key")


class InvalidChangeReason(str, Enum):
    MIXED_MARKDOWN_RENAME = "mixed_markdown_rename"
    RENAME_CROSSES_LOCALES = "rename_crosses_locales"


class MetadataConflictReason(str, Enum):
    SAME_LOCALE_PAIR = "same_locale_pair"
    REUSED_PHYSICAL_PATH = "reused_physical_path"
    DIFFERENT_RENAME_ORIGIN = "different_rename_origin"


class PairDiscoveryError(ValueError):
    """Base class for changed-pair discovery errors."""


class InvalidChangedFileMetadata(PairDiscoveryError):
    change: ChangedFileMetadata
    reason: InvalidChangeReason

    __slots__ = ("change", "reason")

    def __init__(
        self, change: ChangedFileMetadata, reason: InvalidChangeReason, /
    ) -> None:
        _exact(change, ChangedFileMetadata, "InvalidChangedFileMetadata", "change")
        _exact(reason, InvalidChangeReason, "InvalidChangedFileMetadata", "reason")
        self.change = change
        self.reason = reason
        super().__init__(f"invalid_changed_file_metadata:{reason.value}")


class ConflictingChangedFileMetadata(PairDiscoveryError):
    first: ChangedMarkdownFile
    second: ChangedMarkdownFile
    reason: MetadataConflictReason

    __slots__ = ("first", "reason", "second")

    def __init__(
        self,
        first: ChangedMarkdownFile,
        second: ChangedMarkdownFile,
        reason: MetadataConflictReason,
        /,
    ) -> None:
        _exact(first, ChangedMarkdownFile, "ConflictingChangedFileMetadata", "first")
        _exact(second, ChangedMarkdownFile, "ConflictingChangedFileMetadata", "second")
        _exact(reason, MetadataConflictReason, "ConflictingChangedFileMetadata", "reason")
        self.first = first
        self.second = second
        self.reason = reason
        super().__init__(f"conflicting_changed_file_metadata:{reason.value}")


def classify_changed_file(
    roots: LocaleRoots, change: ChangedFileMetadata, /
) -> ChangedMarkdownFile | None:
    _exact(roots, LocaleRoots, "classify_changed_file", "roots")
    _exact(change, ChangedFileMetadata, "classify_changed_file", "change")
    present = tuple(path for path in (change.old_path, change.new_path) if path is not None)
    markdown = tuple(is_markdown_path(path) for path in present)
    if not any(markdown):
        return None
    if change.kind is ChangedFileKind.RENAMED and not all(markdown):
        raise InvalidChangedFileMetadata(change, InvalidChangeReason.MIXED_MARKDOWN_RENAME)
    located = tuple(locate_markdown_path(roots, path) for path in present)
    if change.kind is ChangedFileKind.RENAMED:
        old, new = located
        if old.locale is not new.locale:
            raise InvalidChangedFileMetadata(change, InvalidChangeReason.RENAME_CROSSES_LOCALES)
        return ChangedMarkdownFile(
            roots,
            change.kind,
            new.locale,
            new.key,
            change.old_path,
            change.new_path,
            old.key,
            change.rename_content_state,
        )
    current = located[-1] if change.new_path is not None else located[0]
    return ChangedMarkdownFile(
        roots,
        change.kind,
        current.locale,
        current.key,
        change.old_path,
        change.new_path,
        None,
        None,
    )


class PairFileState(str, Enum):
    BOTH_PRESENT = "both_present"
    RU_ONLY = "ru_only"
    EN_ONLY = "en_only"
    BOTH_MISSING = "both_missing"


@dataclass(frozen=True, slots=True)
class SnapshotLocaleFile:
    roots: LocaleRoots
    locale: Locale
    key: PairKey
    path: RepoPath
    snapshot: SnapshotRef
    content: bytes | None

    def __post_init__(self) -> None:
        type_name = "SnapshotLocaleFile"
        _exact(self.roots, LocaleRoots, type_name, "roots")
        _exact(self.locale, Locale, type_name, "locale")
        _exact(self.key, PairKey, type_name, "key")
        _exact(self.path, RepoPath, type_name, "path")
        _exact(self.snapshot, SnapshotRef, type_name, "snapshot")
        if self.content is not None and type(self.content) is not bytes:
            raise _invariant(type_name, "content", "None or exact bytes")
        root = self.roots.ru if self.locale is Locale.RU else self.roots.en
        if self.path != _join(root, self.key):
            raise _invariant(type_name, "path", "the canonical locale path for key")

    @property
    def exists(self) -> bool:
        return self.content is not None


def _state(ru_exists: bool, en_exists: bool) -> PairFileState:
    if ru_exists and en_exists:
        return PairFileState.BOTH_PRESENT
    if ru_exists:
        return PairFileState.RU_ONLY
    if en_exists:
        return PairFileState.EN_ONLY
    return PairFileState.BOTH_MISSING


@dataclass(frozen=True, slots=True)
class LocalePairInventory:
    roots: LocaleRoots
    key: PairKey
    ru: SnapshotLocaleFile
    en: SnapshotLocaleFile
    changes: tuple[ChangedMarkdownFile, ...]
    state: PairFileState

    def __post_init__(self) -> None:
        type_name = "LocalePairInventory"
        _exact(self.roots, LocaleRoots, type_name, "roots")
        _exact(self.key, PairKey, type_name, "key")
        _exact(self.ru, SnapshotLocaleFile, type_name, "ru")
        _exact(self.en, SnapshotLocaleFile, type_name, "en")
        _exact(self.changes, tuple, type_name, "changes")
        _exact(self.state, PairFileState, type_name, "state")
        if self.ru.roots != self.roots or self.en.roots != self.roots:
            raise _invariant(type_name, "roots", "the same roots on both sides")
        if self.ru.locale is not Locale.RU or self.en.locale is not Locale.EN:
            raise _invariant(type_name, "ru", "canonical RU and EN sides")
        if self.ru.key != self.key or self.en.key != self.key:
            raise _invariant(type_name, "key", "the same key on both sides")
        if self.ru.snapshot != self.en.snapshot:
            raise _invariant(type_name, "en", "the same snapshot on both sides")
        if len(self.changes) > 2:
            raise _invariant(type_name, "changes", "at most one change per locale")
        locales: list[Locale] = []
        for change in self.changes:
            _exact(change, ChangedMarkdownFile, type_name, "changes")
            if change.roots != self.roots or change.key != self.key:
                raise _invariant(type_name, "changes", "matching roots and key")
            locales.append(change.locale)
        if locales != sorted(set(locales), key=lambda locale: 0 if locale is Locale.RU else 1):
            raise _invariant(type_name, "changes", "unique RU-then-EN order")
        if self.state is not _state(self.ru.exists, self.en.exists):
            raise _invariant(type_name, "state", "existence-derived pair state")

    @property
    def changed_locales(self) -> tuple[Locale, ...]:
        return tuple(change.locale for change in self.changes)

    @property
    def is_complete(self) -> bool:
        return self.state is PairFileState.BOTH_PRESENT

    @property
    def is_one_sided(self) -> bool:
        return self.state in {PairFileState.RU_ONLY, PairFileState.EN_ONLY}


def _raw_sort_key(change: ChangedFileMetadata) -> tuple[str, str, str, str]:
    return (
        change.kind.value,
        change.old_path.value if change.old_path is not None else "",
        change.new_path.value if change.new_path is not None else "",
        change.rename_content_state.value if change.rename_content_state is not None else "",
    )


def _classified_sort_key(
    change: ChangedMarkdownFile,
) -> tuple[str, int, str, str, str, str]:
    return (
        change.key.relative_path.value,
        0 if change.locale is Locale.RU else 1,
        change.kind.value,
        change.old_path.value if change.old_path is not None else "",
        change.new_path.value if change.new_path is not None else "",
        change.rename_content_state.value if change.rename_content_state is not None else "",
    )


def _physical_paths(change: ChangedMarkdownFile) -> frozenset[RepoPath]:
    return frozenset(path for path in (change.old_path, change.new_path) if path is not None)


def _conflict_reason(
    first: ChangedMarkdownFile, second: ChangedMarkdownFile
) -> MetadataConflictReason | None:
    if (first.key, first.locale) == (second.key, second.locale):
        return MetadataConflictReason.SAME_LOCALE_PAIR
    if _physical_paths(first) & _physical_paths(second):
        return MetadataConflictReason.REUSED_PHYSICAL_PATH
    if (
        first.kind is ChangedFileKind.RENAMED
        and second.kind is ChangedFileKind.RENAMED
        and first.key == second.key
        and first.locale is not second.locale
        and first.previous_key != second.previous_key
    ):
        return MetadataConflictReason.DIFFERENT_RENAME_ORIGIN
    return None


def _raise_selected_conflict(changes: tuple[ChangedMarkdownFile, ...]) -> None:
    candidates: dict[
        MetadataConflictReason,
        list[tuple[tuple[object, ...], ChangedMarkdownFile, ChangedMarkdownFile]],
    ] = {reason: [] for reason in MetadataConflictReason}
    for left, right in combinations(changes, 2):
        reason = _conflict_reason(left, right)
        if reason is None:
            continue
        first, second = sorted((left, right), key=_classified_sort_key)
        pair_key: tuple[object, ...] = (
            _classified_sort_key(first),
            _classified_sort_key(second),
        )
        candidates[reason].append((pair_key, first, second))
    for reason in (
        MetadataConflictReason.SAME_LOCALE_PAIR,
        MetadataConflictReason.REUSED_PHYSICAL_PATH,
        MetadataConflictReason.DIFFERENT_RENAME_ORIGIN,
    ):
        if candidates[reason]:
            _, first, second = min(candidates[reason], key=lambda candidate: candidate[0])
            raise ConflictingChangedFileMetadata(first, second, reason)


def discover_changed_pairs(
    reader: SnapshotReader,
    snapshots: ResolvedRepositorySnapshots,
    roots: LocaleRoots,
    changes: tuple[ChangedFileMetadata, ...],
    /,
) -> tuple[LocalePairInventory, ...]:
    _exact(snapshots, ResolvedRepositorySnapshots, "discover_changed_pairs", "snapshots")
    _exact(roots, LocaleRoots, "discover_changed_pairs", "roots")
    _exact(changes, tuple, "discover_changed_pairs", "changes")
    for supplied_change in changes:
        _exact(supplied_change, ChangedFileMetadata, "discover_changed_pairs", "changes")

    unique = sorted(set(changes), key=_raw_sort_key)
    classified_items: list[ChangedMarkdownFile] = []
    for raw_change in unique:
        item = classify_changed_file(roots, raw_change)
        if item is not None:
            classified_items.append(item)
    classified = tuple(classified_items)
    _raise_selected_conflict(classified)

    grouped: dict[PairKey, list[ChangedMarkdownFile]] = {}
    for classified_change in classified:
        grouped.setdefault(classified_change.key, []).append(classified_change)

    inventories: list[LocalePairInventory] = []
    for key in sorted(grouped, key=lambda item: item.relative_path.value):
        ru_path = _join(roots.ru, key)
        en_path = _join(roots.en, key)
        ru_content = reader.read_bytes(snapshots.scope_snapshot, ru_path)
        en_content = reader.read_bytes(snapshots.scope_snapshot, en_path)
        ru = SnapshotLocaleFile(
            roots, Locale.RU, key, ru_path, snapshots.scope_snapshot, ru_content
        )
        en = SnapshotLocaleFile(
            roots, Locale.EN, key, en_path, snapshots.scope_snapshot, en_content
        )
        pair_changes = tuple(
            sorted(grouped[key], key=lambda item: 0 if item.locale is Locale.RU else 1)
        )
        inventories.append(
            LocalePairInventory(
                roots,
                key,
                ru,
                en,
                pair_changes,
                _state(ru.exists, en.exists),
            )
        )
    return tuple(inventories)
