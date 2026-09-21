from dataclasses import FrozenInstanceError
from hashlib import sha256

import pytest

from ydbdoc_review_ng.domain import ContentHash, GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.errors import InvariantViolation, UnknownDomainType
from ydbdoc_review_ng.provenance import (
    IncompatibleHistory,
    PathHistoryState,
    PathProvenance,
    ProvenanceAssessment,
    ProvenancePath,
    ProvenanceRole,
    compare_provenance,
)

REPOSITORY = RepositoryId("owner/repo")
BASELINE = SnapshotRef(REPOSITORY, GitSha("1" * 40))
CURRENT = SnapshotRef(REPOSITORY, GitSha("2" * 40))
P1 = RepoPath("ru/a.md")
P2 = RepoPath("en/a.md")
C1 = GitSha("3" * 40)
C2 = GitSha("4" * 40)


class RepoPathSubclass(RepoPath):
    pass


class ContentHashSubclass(ContentHash):
    pass


class GitShaSubclass(GitSha):
    pass


class SnapshotRefSubclass(SnapshotRef):
    pass


class PathProvenanceSubclass(PathProvenance):
    pass


class TupleSubclass(tuple[object, ...]):
    pass


class StringSubclass(str):
    pass


class FakeRepository:
    def __init__(self, ancestor: bool, contents: dict[tuple[str, str], bytes | None], commits: tuple[GitSha, ...] = ()) -> None:
        self._repository = REPOSITORY
        self.ancestor = ancestor
        self.contents = contents
        self.commits = commits
        self.calls: list[tuple[object, ...]] = []

    def is_ancestor(self, older: SnapshotRef, newer: SnapshotRef, /) -> bool:
        self.calls.append(("is_ancestor", older, newer))
        return self.ancestor

    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
        self.calls.append(("read_bytes", snapshot, path))
        return self.contents[(snapshot.commit_sha.value, path.value)]

    def commits_touching(self, older: SnapshotRef, newer: SnapshotRef, path: RepoPath, /) -> tuple[GitSha, ...]:
        self.calls.append(("commits_touching", older, newer, path))
        return self.commits


def content(value: bytes) -> ContentHash:
    return ContentHash(sha256(value).hexdigest())


@pytest.mark.parametrize(
    "old,new,state,old_id,current_id",
    [
        (b"same", b"same", PathHistoryState.UNCHANGED, content(b"same"), content(b"same")),
        (None, None, PathHistoryState.UNCHANGED, None, None),
        (b"old", b"new", PathHistoryState.NEWER, content(b"old"), content(b"new")),
        (None, b"new", PathHistoryState.CREATED, None, content(b"new")),
        (b"old", None, PathHistoryState.DELETED, content(b"old"), None),
        (None, b"", PathHistoryState.CREATED, None, content(b"")),
    ],
)
@pytest.mark.parametrize("role,path", [(ProvenanceRole.SOURCE, P1), (ProvenanceRole.TARGET, P2)])
def test_provenance_content_matrix(
    role: ProvenanceRole,
    path: RepoPath,
    old: bytes | None,
    new: bytes | None,
    state: PathHistoryState,
    old_id: ContentHash | None,
    current_id: ContentHash | None,
) -> None:
    repository = FakeRepository(
        True,
        {(BASELINE.commit_sha.value, path.value): old, (CURRENT.commit_sha.value, path.value): new},
        (C1, C2),
    )
    result = compare_provenance(repository, BASELINE, CURRENT, (ProvenancePath(role, path),))  # type: ignore[arg-type]
    assert isinstance(result, ProvenanceAssessment)
    assert result.paths == (PathProvenance(role, path, state, old_id, current_id, (C1, C2)),)
    assert result.paths[0].is_warning is (state is not PathHistoryState.UNCHANGED)


def test_touched_then_reverted_keeps_ordered_commits() -> None:
    repository = FakeRepository(
        True,
        {(BASELINE.commit_sha.value, P1.value): b"same", (CURRENT.commit_sha.value, P1.value): b"same"},
        (C1, C2),
    )
    result = compare_provenance(repository, BASELINE, CURRENT, (ProvenancePath(ProvenanceRole.SOURCE, P1),))  # type: ignore[arg-type]
    assert isinstance(result, ProvenanceAssessment)
    assert result.paths[0].state is PathHistoryState.UNCHANGED
    assert result.paths[0].intervening_commits == (C1, C2)


def test_incompatible_history_exits_before_reads() -> None:
    repository = FakeRepository(False, {})
    result = compare_provenance(repository, BASELINE, CURRENT, (ProvenancePath(ProvenanceRole.TARGET, P2),))  # type: ignore[arg-type]
    assert result == IncompatibleHistory(BASELINE, CURRENT, "incompatible_repository_history")
    assert repository.calls == [("is_ancestor", BASELINE, CURRENT)]


ENTRY = PathProvenance(
    ProvenanceRole.SOURCE,
    P1,
    PathHistoryState.NEWER,
    content(b"old"),
    content(b"new"),
    (C1,),
)


@pytest.mark.parametrize(
    "record,field_name",
    [
        (ProvenancePath(ProvenanceRole.SOURCE, P1), "role"),
        (ENTRY, "state"),
        (ProvenanceAssessment(BASELINE, CURRENT, (ENTRY,)), "paths"),
        (
            IncompatibleHistory(BASELINE, CURRENT, "incompatible_repository_history"),
            "code",
        ),
    ],
)
def test_every_provenance_record_rejects_assignment(
    record: object, field_name: str
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(record, field_name, object())


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ProvenancePath("source", P1),
        lambda: ProvenancePath(ProvenanceRole.SOURCE, RepoPathSubclass("ru/a.md")),
        lambda: PathProvenance(
            "source", P1, PathHistoryState.NEWER, content(b"old"), content(b"new"), ()
        ),
        lambda: PathProvenance(
            ProvenanceRole.SOURCE,
            RepoPathSubclass("ru/a.md"),
            PathHistoryState.NEWER,
            content(b"old"),
            content(b"new"),
            (),
        ),
        lambda: PathProvenance(
            ProvenanceRole.SOURCE, P1, "newer", content(b"old"), content(b"new"), ()
        ),
        lambda: PathProvenance(
            ProvenanceRole.SOURCE,
            P1,
            PathHistoryState.NEWER,
            ContentHashSubclass(content(b"old").value),
            content(b"new"),
            (),
        ),
        lambda: PathProvenance(
            ProvenanceRole.SOURCE,
            P1,
            PathHistoryState.NEWER,
            content(b"old"),
            ContentHashSubclass(content(b"new").value),
            (),
        ),
        lambda: PathProvenance(
            ProvenanceRole.SOURCE,
            P1,
            PathHistoryState.NEWER,
            content(b"old"),
            content(b"new"),
            TupleSubclass((C1,)),
        ),
        lambda: PathProvenance(
            ProvenanceRole.SOURCE,
            P1,
            PathHistoryState.NEWER,
            content(b"old"),
            content(b"new"),
            (GitShaSubclass("3" * 40),),
        ),
    ],
)
def test_provenance_path_records_reject_non_exact_runtime_types(factory: object) -> None:
    with pytest.raises(InvariantViolation):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "state,old_id,current_id",
    [
        (PathHistoryState.UNCHANGED, None, content(b"new")),
        (PathHistoryState.UNCHANGED, content(b"old"), None),
        (PathHistoryState.UNCHANGED, content(b"old"), content(b"new")),
        (PathHistoryState.NEWER, None, content(b"new")),
        (PathHistoryState.NEWER, content(b"old"), None),
        (PathHistoryState.NEWER, content(b"same"), content(b"same")),
        (PathHistoryState.CREATED, content(b"old"), content(b"new")),
        (PathHistoryState.CREATED, None, None),
        (PathHistoryState.DELETED, None, content(b"new")),
        (PathHistoryState.DELETED, None, None),
        (PathHistoryState.DELETED, content(b"old"), content(b"new")),
    ],
)
def test_path_provenance_rejects_every_content_state_contradiction(
    state: PathHistoryState,
    old_id: ContentHash | None,
    current_id: ContentHash | None,
) -> None:
    with pytest.raises(InvariantViolation):
        PathProvenance(
            ProvenanceRole.SOURCE, P1, state, old_id, current_id, ()
        )


def test_path_provenance_rejects_duplicate_commits() -> None:
    with pytest.raises(InvariantViolation):
        PathProvenance(
            ProvenanceRole.SOURCE,
            P1,
            PathHistoryState.NEWER,
            content(b"old"),
            content(b"new"),
            (C1, C1),
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ProvenanceAssessment(
            SnapshotRefSubclass(REPOSITORY, GitSha("1" * 40)), CURRENT, (ENTRY,)
        ),
        lambda: ProvenanceAssessment(
            BASELINE, SnapshotRefSubclass(REPOSITORY, GitSha("2" * 40)), (ENTRY,)
        ),
        lambda: ProvenanceAssessment(
            BASELINE,
            SnapshotRef(RepositoryId("other/repo"), GitSha("2" * 40)),
            (ENTRY,),
        ),
        lambda: ProvenanceAssessment(BASELINE, CURRENT, TupleSubclass((ENTRY,))),
        lambda: ProvenanceAssessment(
            BASELINE,
            CURRENT,
            (
                PathProvenanceSubclass(
                    ProvenanceRole.SOURCE,
                    P1,
                    PathHistoryState.NEWER,
                    content(b"old"),
                    content(b"new"),
                    (C1,),
                ),
            ),
        ),
        lambda: ProvenanceAssessment(BASELINE, CURRENT, (ENTRY, ENTRY)),
        lambda: IncompatibleHistory(
            SnapshotRefSubclass(REPOSITORY, GitSha("1" * 40)),
            CURRENT,
            "incompatible_repository_history",
        ),
        lambda: IncompatibleHistory(
            BASELINE,
            SnapshotRefSubclass(REPOSITORY, GitSha("2" * 40)),
            "incompatible_repository_history",
        ),
        lambda: IncompatibleHistory(
            BASELINE,
            SnapshotRef(RepositoryId("other/repo"), GitSha("2" * 40)),
            "incompatible_repository_history",
        ),
        lambda: IncompatibleHistory(BASELINE, CURRENT, "other"),
        lambda: IncompatibleHistory(
            BASELINE, CURRENT, StringSubclass("incompatible_repository_history")
        ),
    ],
)
def test_provenance_result_records_reject_all_type_repository_and_key_contradictions(
    factory: object,
) -> None:
    with pytest.raises(InvariantViolation):
        factory()  # type: ignore[operator]


def test_provenance_enum_wire_values_are_exact() -> None:
    assert tuple(role.value for role in ProvenanceRole) == ("source", "target")
    assert tuple(state.value for state in PathHistoryState) == (
        "unchanged",
        "newer",
        "created",
        "deleted",
    )


def test_t003_records_are_not_added_to_t002_wire_codec() -> None:
    from ydbdoc_review_ng.domain import DOMAIN_SCHEMA_VERSION, to_wire

    assert DOMAIN_SCHEMA_VERSION == 1
    with pytest.raises(UnknownDomainType):
        to_wire(ProvenancePath(ProvenanceRole.SOURCE, P1))  # type: ignore[arg-type]
