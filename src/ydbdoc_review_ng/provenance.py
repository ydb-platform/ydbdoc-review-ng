"""Post-merge path provenance over immutable repository snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import TypeAlias

from ydbdoc_review_ng.domain import ContentHash, GitSha, RepoPath, SnapshotRef
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.repository import GitRepository, RepositoryMismatch

__all__ = [
    "IncompatibleHistory",
    "PathHistoryState",
    "PathProvenance",
    "ProvenanceAssessment",
    "ProvenancePath",
    "ProvenanceResult",
    "ProvenanceRole",
    "compare_provenance",
]


def _invariant(type_name: str, field_name: str, expectation: str) -> InvariantViolation:
    return InvariantViolation(f"{type_name}.{field_name}: expected {expectation}")


def _exact(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    if type(value) is not expected:
        raise _invariant(type_name, field_name, f"exact {expected.__name__}")


def _optional_exact(value: object, expected: type[object], type_name: str, field_name: str) -> None:
    if value is not None and type(value) is not expected:
        raise _invariant(type_name, field_name, f"None or exact {expected.__name__}")


class ProvenanceRole(str, Enum):
    SOURCE = "source"
    TARGET = "target"


class PathHistoryState(str, Enum):
    UNCHANGED = "unchanged"
    NEWER = "newer"
    CREATED = "created"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class ProvenancePath:
    role: ProvenanceRole
    path: RepoPath

    def __post_init__(self) -> None:
        _exact(self.role, ProvenanceRole, "ProvenancePath", "role")
        _exact(self.path, RepoPath, "ProvenancePath", "path")


@dataclass(frozen=True, slots=True)
class PathProvenance:
    role: ProvenanceRole
    path: RepoPath
    state: PathHistoryState
    old_content_id: ContentHash | None
    current_content_id: ContentHash | None
    intervening_commits: tuple[GitSha, ...]

    def __post_init__(self) -> None:
        type_name = "PathProvenance"
        _exact(self.role, ProvenanceRole, type_name, "role")
        _exact(self.path, RepoPath, type_name, "path")
        _exact(self.state, PathHistoryState, type_name, "state")
        _optional_exact(self.old_content_id, ContentHash, type_name, "old_content_id")
        _optional_exact(self.current_content_id, ContentHash, type_name, "current_content_id")
        _exact(self.intervening_commits, tuple, type_name, "intervening_commits")
        if any(type(commit) is not GitSha for commit in self.intervening_commits):
            raise _invariant(type_name, "intervening_commits", "a tuple of exact GitSha values")
        if len(set(self.intervening_commits)) != len(self.intervening_commits):
            raise _invariant(type_name, "intervening_commits", "unique GitSha values")

        old = self.old_content_id
        current = self.current_content_id
        valid = {
            PathHistoryState.UNCHANGED: (old is None and current is None)
            or (old is not None and current is not None and old == current),
            PathHistoryState.NEWER: old is not None and current is not None and old != current,
            PathHistoryState.CREATED: old is None and current is not None,
            PathHistoryState.DELETED: old is not None and current is None,
        }
        if not valid[self.state]:
            raise _invariant(type_name, "state", "the exact content-ID state table")

    @property
    def is_warning(self) -> bool:
        return self.state is not PathHistoryState.UNCHANGED


@dataclass(frozen=True, slots=True)
class ProvenanceAssessment:
    baseline_snapshot: SnapshotRef
    current_snapshot: SnapshotRef
    paths: tuple[PathProvenance, ...]

    def __post_init__(self) -> None:
        type_name = "ProvenanceAssessment"
        _exact(self.baseline_snapshot, SnapshotRef, type_name, "baseline_snapshot")
        _exact(self.current_snapshot, SnapshotRef, type_name, "current_snapshot")
        if self.baseline_snapshot.repository != self.current_snapshot.repository:
            raise _invariant(type_name, "current_snapshot", "the baseline repository")
        _exact(self.paths, tuple, type_name, "paths")
        if any(type(path) is not PathProvenance for path in self.paths):
            raise _invariant(type_name, "paths", "a tuple of exact PathProvenance values")
        keys = tuple((path.role, path.path) for path in self.paths)
        if len(set(keys)) != len(keys):
            raise _invariant(type_name, "paths", "unique role/path keys")


@dataclass(frozen=True, slots=True)
class IncompatibleHistory:
    baseline_snapshot: SnapshotRef
    current_snapshot: SnapshotRef
    code: str

    def __post_init__(self) -> None:
        type_name = "IncompatibleHistory"
        _exact(self.baseline_snapshot, SnapshotRef, type_name, "baseline_snapshot")
        _exact(self.current_snapshot, SnapshotRef, type_name, "current_snapshot")
        if self.baseline_snapshot.repository != self.current_snapshot.repository:
            raise _invariant(type_name, "current_snapshot", "the baseline repository")
        if type(self.code) is not str or self.code != "incompatible_repository_history":
            raise _invariant(type_name, "code", "the literal incompatible_repository_history")


ProvenanceResult: TypeAlias = ProvenanceAssessment | IncompatibleHistory


def _content_id(value: bytes | None) -> ContentHash | None:
    return None if value is None else ContentHash(sha256(value).hexdigest())


def _state(old: ContentHash | None, current: ContentHash | None) -> PathHistoryState:
    if old is None and current is not None:
        return PathHistoryState.CREATED
    if old is not None and current is None:
        return PathHistoryState.DELETED
    if old == current:
        return PathHistoryState.UNCHANGED
    return PathHistoryState.NEWER


def compare_provenance(
    repository: GitRepository,
    baseline_snapshot: SnapshotRef,
    current_snapshot: SnapshotRef,
    paths: tuple[ProvenancePath, ...],
    /,
) -> ProvenanceResult:
    _exact(baseline_snapshot, SnapshotRef, "compare_provenance", "baseline_snapshot")
    _exact(current_snapshot, SnapshotRef, "compare_provenance", "current_snapshot")
    if baseline_snapshot.repository != current_snapshot.repository:
        raise RepositoryMismatch(baseline_snapshot.repository, current_snapshot.repository)
    if repository._repository != baseline_snapshot.repository:
        raise RepositoryMismatch(repository._repository, baseline_snapshot.repository)
    _exact(paths, tuple, "compare_provenance", "paths")
    if any(type(path) is not ProvenancePath for path in paths):
        raise _invariant("compare_provenance", "paths", "a tuple of exact ProvenancePath values")
    keys = tuple((path.role, path.path) for path in paths)
    if len(set(keys)) != len(keys):
        raise _invariant("compare_provenance", "paths", "unique role/path keys")

    if not repository.is_ancestor(baseline_snapshot, current_snapshot):
        return IncompatibleHistory(
            baseline_snapshot, current_snapshot, "incompatible_repository_history"
        )

    results: list[PathProvenance] = []
    for requested_path in paths:
        old = _content_id(repository.read_bytes(baseline_snapshot, requested_path.path))
        current = _content_id(repository.read_bytes(current_snapshot, requested_path.path))
        commits = repository.commits_touching(
            baseline_snapshot, current_snapshot, requested_path.path
        )
        results.append(
            PathProvenance(
                requested_path.role,
                requested_path.path,
                _state(old, current),
                old,
                current,
                commits,
            )
        )
    return ProvenanceAssessment(baseline_snapshot, current_snapshot, tuple(results))
