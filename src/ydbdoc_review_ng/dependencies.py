"""Immutable dependency and redirect vocabulary for frozen translation scopes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ydbdoc_review_ng.domain import Locale, RepoPath, SnapshotRef
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.locales import LocaleRoots, locate_markdown_path

__all__ = [
    "DependencyError",
    "DependencyInputReason",
    "DependencyLink",
    "DependencyResolutionState",
    "DependencySource",
    "InvalidDependencyInput",
    "RedirectCatalog",
    "RedirectCycleDetected",
    "RedirectEntry",
    "ResolvedDependency",
    "resolve_redirect",
]


def _invariant(type_name: str, field_name: str, expectation: str) -> InvariantViolation:
    return InvariantViolation(f"{type_name}.{field_name}: expected {expectation}")


def _exact(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    if type(value) is not expected:
        raise _invariant(type_name, field_name, f"exact {expected.__name__}")


def _markdown(path: RepoPath) -> bool:
    return path.value.rsplit("/", 1)[-1].endswith(".md")


class DependencyInputReason(str, Enum):
    INVALID_SCANNER_RESULT = "invalid_scanner_result"
    SOURCE_PATH_MISMATCH = "source_path_mismatch"
    NON_MARKDOWN_DESTINATION = "non_markdown_destination"
    MIXED_LOCALE = "mixed_locale"
    PATH_OUTSIDE_SOURCE_LOCALE = "path_outside_source_locale"
    NON_MARKDOWN_REDIRECT = "non_markdown_redirect"
    DUPLICATE_REDIRECT_FROM = "duplicate_redirect_from"
    REDIRECT_CROSSES_LOCALES = "redirect_crosses_locales"
    REDIRECT_SELF_LOOP = "redirect_self_loop"


class DependencyResolutionState(str, Enum):
    TARGET_EXISTS = "target_exists"
    TARGET_REDIRECT_EXISTS = "target_redirect_exists"
    TARGET_MISSING_SOURCE_EXISTS = "target_missing_source_exists"
    TARGET_MISSING_ANCHOR_SOURCE_EXISTS = "target_missing_anchor_source_exists"
    SOURCE_MISSING = "source_missing"


@dataclass(frozen=True, slots=True)
class DependencyLink:
    source_path: RepoPath
    destination_source_path: RepoPath
    fragment: str | None = None

    def __post_init__(self) -> None:
        _exact(self.source_path, RepoPath, "DependencyLink", "source_path")
        _exact(
            self.destination_source_path,
            RepoPath,
            "DependencyLink",
            "destination_source_path",
        )
        if self.fragment is not None:
            _exact(self.fragment, str, "DependencyLink", "fragment")
            if not self.fragment:
                raise _invariant("DependencyLink", "fragment", "None or non-empty string")


@dataclass(frozen=True, slots=True)
class RedirectEntry:
    from_path: RepoPath
    to_path: RepoPath

    def __post_init__(self) -> None:
        _exact(self.from_path, RepoPath, "RedirectEntry", "from_path")
        _exact(self.to_path, RepoPath, "RedirectEntry", "to_path")


class DependencyError(ValueError):
    """Base class for dependency-boundary failures."""


class InvalidDependencyInput(DependencyError):
    reason: DependencyInputReason
    path: RepoPath | None

    def __init__(self, reason: DependencyInputReason, path: RepoPath | None, /) -> None:
        _exact(reason, DependencyInputReason, "InvalidDependencyInput", "reason")
        if path is not None:
            _exact(path, RepoPath, "InvalidDependencyInput", "path")
        self.reason = reason
        self.path = path
        super().__init__(f"invalid_dependency_input:{reason.value}")


class RedirectCycleDetected(DependencyError):
    locale: Locale
    paths: tuple[RepoPath, ...]

    def __init__(self, locale: Locale, paths: tuple[RepoPath, ...], /) -> None:
        _exact(locale, Locale, "RedirectCycleDetected", "locale")
        _exact(paths, tuple, "RedirectCycleDetected", "paths")
        for path in paths:
            _exact(path, RepoPath, "RedirectCycleDetected", "paths")
        self.locale = locale
        self.paths = paths
        super().__init__("redirect_cycle_detected")


def _located_locale(roots: LocaleRoots, path: RepoPath) -> Locale | None:
    try:
        return locate_markdown_path(roots, path).locale
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RedirectCatalog:
    snapshot: SnapshotRef
    roots: LocaleRoots
    entries: tuple[RedirectEntry, ...]

    def __post_init__(self) -> None:
        _exact(self.snapshot, SnapshotRef, "RedirectCatalog", "snapshot")
        _exact(self.roots, LocaleRoots, "RedirectCatalog", "roots")
        _exact(self.entries, tuple, "RedirectCatalog", "entries")
        for entry in self.entries:
            _exact(entry, RedirectEntry, "RedirectCatalog", "entries")

        non_markdown = sorted(
            (
                path
                for entry in self.entries
                for path in (entry.from_path, entry.to_path)
                if not _markdown(path)
            ),
            key=lambda path: path.value,
        )
        if non_markdown:
            raise InvalidDependencyInput(
                DependencyInputReason.NON_MARKDOWN_REDIRECT, non_markdown[0]
            )

        located = {
            path: _located_locale(self.roots, path)
            for entry in self.entries
            for path in (entry.from_path, entry.to_path)
        }
        outside = sorted(
            (path for path, locale in located.items() if locale is None), key=lambda p: p.value
        )
        if outside:
            raise InvalidDependencyInput(
                DependencyInputReason.PATH_OUTSIDE_SOURCE_LOCALE, outside[0]
            )
        crossing = sorted(
            (
                entry.from_path
                for entry in self.entries
                if located[entry.from_path] is not located[entry.to_path]
            ),
            key=lambda path: path.value,
        )
        if crossing:
            raise InvalidDependencyInput(
                DependencyInputReason.REDIRECT_CROSSES_LOCALES, crossing[0]
            )
        self_loops = sorted(
            (entry.from_path for entry in self.entries if entry.from_path == entry.to_path),
            key=lambda path: path.value,
        )
        if self_loops:
            raise InvalidDependencyInput(DependencyInputReason.REDIRECT_SELF_LOOP, self_loops[0])
        counts: dict[RepoPath, int] = {}
        for entry in self.entries:
            counts[entry.from_path] = counts.get(entry.from_path, 0) + 1
        repeated = sorted(
            (path for path, count in counts.items() if count > 1), key=lambda p: p.value
        )
        if repeated:
            raise InvalidDependencyInput(DependencyInputReason.DUPLICATE_REDIRECT_FROM, repeated[0])
        object.__setattr__(
            self,
            "entries",
            tuple(
                sorted(self.entries, key=lambda item: (item.from_path.value, item.to_path.value))
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    link: DependencyLink
    source_path: RepoPath
    target_path: RepoPath
    state: DependencyResolutionState

    def __post_init__(self) -> None:
        _exact(self.link, DependencyLink, "ResolvedDependency", "link")
        _exact(self.source_path, RepoPath, "ResolvedDependency", "source_path")
        _exact(self.target_path, RepoPath, "ResolvedDependency", "target_path")
        _exact(self.state, DependencyResolutionState, "ResolvedDependency", "state")


class DependencySource(Protocol):
    def links(
        self,
        snapshot: SnapshotRef,
        source_path: RepoPath,
        source_content: bytes,
        /,
    ) -> tuple[DependencyLink, ...]: ...


def resolve_redirect(catalog: RedirectCatalog, locale: Locale, path: RepoPath, /) -> RepoPath:
    _exact(catalog, RedirectCatalog, "resolve_redirect", "catalog")
    _exact(locale, Locale, "resolve_redirect", "locale")
    _exact(path, RepoPath, "resolve_redirect", "path")
    if not _markdown(path):
        raise InvalidDependencyInput(DependencyInputReason.NON_MARKDOWN_DESTINATION, path)
    located = _located_locale(catalog.roots, path)
    if located is not None and located is not locale:
        raise InvalidDependencyInput(DependencyInputReason.MIXED_LOCALE, path)
    if located is None:
        raise InvalidDependencyInput(DependencyInputReason.PATH_OUTSIDE_SOURCE_LOCALE, path)

    redirects = {entry.from_path: entry.to_path for entry in catalog.entries}
    chain: list[RepoPath] = []
    positions: dict[RepoPath, int] = {}
    current = path
    while current in redirects:
        if current in positions:
            cycle = chain[positions[current] :]
            start = min(range(len(cycle)), key=lambda index: cycle[index].value)
            canonical = cycle[start:] + cycle[:start]
            raise RedirectCycleDetected(locale, (*canonical, canonical[0]))
        positions[current] = len(chain)
        chain.append(current)
        current = redirects[current]
    return current
