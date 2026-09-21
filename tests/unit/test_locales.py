from __future__ import annotations

import ast
import hashlib
import inspect
import itertools
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from enum import Enum
from pathlib import Path
from typing import cast

import pytest

from ydbdoc_review_ng.domain import (
    DOMAIN_SCHEMA_VERSION,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    SnapshotRef,
    to_wire,
)
from ydbdoc_review_ng.errors import InvariantViolation, UnknownDomainType
from ydbdoc_review_ng.locales import (
    ChangedFileKind,
    ChangedFileMetadata,
    ChangedMarkdownFile,
    ConflictingChangedFileMetadata,
    InvalidChangedFileMetadata,
    InvalidChangeReason,
    LocalePairInventory,
    LocalePathError,
    LocaleRoots,
    LocalizedMarkdownPath,
    MetadataConflictReason,
    NonMarkdownPath,
    PairDiscoveryError,
    PairFileState,
    PairKey,
    PathOutsideLocaleRoots,
    RenameContentState,
    SnapshotLocaleFile,
    classify_changed_file,
    discover_changed_pairs,
    is_markdown_path,
    locate_markdown_path,
    paired_markdown_path,
)
from ydbdoc_review_ng.repository import (
    BaseBranch,
    PullRequestState,
    ResolvedRepositorySnapshots,
)

ROOTS = LocaleRoots(RepoPath("ydb/docs/ru"), RepoPath("ydb/docs/en"))
SNAPSHOT = SnapshotRef(
    RepositoryId("ydb-platform/ydb"),
    GitSha("5aab6d0e65926540eff571d03a018a492face7e1"),
)
MERGED_PR_SNAPSHOT = SnapshotRef(
    SNAPSHOT.repository,
    GitSha("b" * 40),
)
MERGED_SCOPE_SNAPSHOT = SnapshotRef(
    SNAPSHOT.repository,
    GitSha("c" * 40),
)


class LedgerReader:
    def __init__(self, content: dict[RepoPath, bytes | None] | None = None) -> None:
        self.content = content or {}
        self.calls: list[tuple[SnapshotRef, RepoPath]] = []
        self.head = "moving-head"
        self.worktree = "mutable-worktree"

    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
        self.calls.append((snapshot, path))
        return self.content.get(path)


def snapshots(scope: SnapshotRef = SNAPSHOT) -> ResolvedRepositorySnapshots:
    return ResolvedRepositorySnapshots(
        PullRequestState.OPEN,
        BaseBranch("main"),
        scope,
        scope,
        scope,
        scope,
        SnapshotRef(scope.repository, GitSha("a" * 40)),
        None,
        None,
    )


def merged_snapshots() -> ResolvedRepositorySnapshots:
    return ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        MERGED_PR_SNAPSHOT,
        MERGED_SCOPE_SNAPSHOT,
        MERGED_SCOPE_SNAPSHOT,
        MERGED_SCOPE_SNAPSHOT,
        MERGED_SCOPE_SNAPSHOT,
        MERGED_SCOPE_SNAPSHOT,
        MERGED_PR_SNAPSHOT,
    )


def added(path: str) -> ChangedFileMetadata:
    return ChangedFileMetadata(ChangedFileKind.ADDED, None, RepoPath(path), None)


def modified(path: str) -> ChangedFileMetadata:
    repo_path = RepoPath(path)
    return ChangedFileMetadata(ChangedFileKind.MODIFIED, repo_path, repo_path, None)


def deleted(path: str) -> ChangedFileMetadata:
    return ChangedFileMetadata(ChangedFileKind.DELETED, RepoPath(path), None, None)


def renamed(old: str, new: str, state: RenameContentState) -> ChangedFileMetadata:
    return ChangedFileMetadata(ChangedFileKind.RENAMED, RepoPath(old), RepoPath(new), state)


def canonical_paths(key: str) -> tuple[RepoPath, RepoPath]:
    return RepoPath(f"ydb/docs/ru/{key}"), RepoPath(f"ydb/docs/en/{key}")


def duplicate(change: ChangedFileMetadata) -> ChangedFileMetadata:
    return ChangedFileMetadata(
        change.kind,
        change.old_path,
        change.new_path,
        change.rename_content_state,
    )


def test_a002_explicit_roots_reject_equal_nested_and_string_prefix_lookalikes() -> None:
    assert [field.name for field in fields(LocaleRoots)] == ["ru", "en"]
    with pytest.raises(InvariantViolation):
        LocaleRoots(RepoPath("docs"), RepoPath("docs"))
    with pytest.raises(InvariantViolation):
        LocaleRoots(RepoPath("docs"), RepoPath("docs/en"))
    with pytest.raises(InvariantViolation):
        LocaleRoots(RepoPath("docs/en"), RepoPath("docs"))
    with pytest.raises(PathOutsideLocaleRoots):
        locate_markdown_path(ROOTS, RepoPath("ydb/docs/russian/page.md"))


@pytest.mark.parametrize(
    ("suffix", "ru", "en"),
    [
        ("index.md", "ydb/docs/ru/index.md", "ydb/docs/en/index.md"),
        ("core/topic.md", "ydb/docs/ru/core/topic.md", "ydb/docs/en/core/topic.md"),
        ("a space/page.md", "ydb/docs/ru/a space/page.md", "ydb/docs/en/a space/page.md"),
        ("-flags/-x.md", "ydb/docs/ru/-flags/-x.md", "ydb/docs/en/-flags/-x.md"),
        ("ядро/Страница.md", "ydb/docs/ru/ядро/Страница.md", "ydb/docs/en/ядро/Страница.md"),
    ],
)
def test_a003_a004_mapping_is_symmetric_from_each_locale(
    suffix: str, ru: str, en: str
) -> None:
    key = PairKey(RepoPath(suffix))
    ru_path, en_path = RepoPath(ru), RepoPath(en)
    assert locate_markdown_path(ROOTS, ru_path) == LocalizedMarkdownPath(
        ROOTS, Locale.RU, ru_path, key
    )
    assert paired_markdown_path(ROOTS, ru_path) == en_path
    assert paired_markdown_path(ROOTS, paired_markdown_path(ROOTS, ru_path)) == ru_path
    assert locate_markdown_path(ROOTS, en_path) == LocalizedMarkdownPath(
        ROOTS, Locale.EN, en_path, key
    )
    assert paired_markdown_path(ROOTS, en_path) == ru_path
    assert paired_markdown_path(ROOTS, paired_markdown_path(ROOTS, en_path)) == en_path


@pytest.mark.parametrize("value", [".md", "page.md", "dir.md/page.md"])
def test_a006_markdown_is_a_case_sensitive_final_component_suffix(value: str) -> None:
    assert is_markdown_path(RepoPath(value))


@pytest.mark.parametrize("value", ["page.MD", "page.markdown", "page.md.tmp", "dir.md/child"])
def test_a006_non_markdown_forms_are_rejected(value: str) -> None:
    assert not is_markdown_path(RepoPath(value))
    with pytest.raises(NonMarkdownPath):
        locate_markdown_path(ROOTS, RepoPath(value))


def test_a005_invalid_paths_fail_closed_and_exceptions_do_not_echo() -> None:
    with pytest.raises(NonMarkdownPath):
        paired_markdown_path(ROOTS, ROOTS.ru)
    with pytest.raises(PathOutsideLocaleRoots):
        paired_markdown_path(ROOTS, RepoPath("other/docs/page.md"))
    for invalid in ("/absolute.md", "a\\b.md", "a//b.md", "a/./b.md", "a/../b.md"):
        with pytest.raises(InvariantViolation):
            RepoPath(invalid)
    canary = RepoPath("-x/$HOME/*\n?.md")
    for error_type, fixed in (
        (NonMarkdownPath, "non_markdown_path"),
        (PathOutsideLocaleRoots, "path_outside_locale_roots"),
    ):
        error = error_type(canary)
        assert error.path is canary
        assert error.args == (fixed,)
        assert str(error) == fixed
        assert repr(error) == f"{error_type.__name__}({fixed!r})"
        assert canary.value not in repr(error)
        with pytest.raises(InvariantViolation):
            error_type(cast(RepoPath, canary.value))


@pytest.mark.parametrize(
    ("change", "kind", "locale", "key"),
    [
        (added("ydb/docs/ru/new.md"), ChangedFileKind.ADDED, Locale.RU, "new.md"),
        (added("ydb/docs/en/new.md"), ChangedFileKind.ADDED, Locale.EN, "new.md"),
        (modified("ydb/docs/ru/edit.md"), ChangedFileKind.MODIFIED, Locale.RU, "edit.md"),
        (modified("ydb/docs/en/edit.md"), ChangedFileKind.MODIFIED, Locale.EN, "edit.md"),
    ],
)
def test_a007_added_and_modified_metadata_are_structural(
    change: ChangedFileMetadata, kind: ChangedFileKind, locale: Locale, key: str
) -> None:
    classified = classify_changed_file(ROOTS, change)
    assert classified is not None
    assert (classified.kind, classified.locale, classified.key.relative_path.value) == (
        kind,
        locale,
        key,
    )
    assert (classified.old_path, classified.new_path) == (change.old_path, change.new_path)


def test_a006_non_markdown_change_is_only_exclusion_and_reads_nothing() -> None:
    reader = LedgerReader()
    assert classify_changed_file(ROOTS, added("ydb/docs/ru/image.png")) is None
    assert discover_changed_pairs(
        reader, snapshots(), ROOTS, (added("ydb/docs/ru/image.png"),)
    ) == ()
    assert reader.calls == []


@pytest.mark.parametrize("content", [None, b"", b"still present"])
@pytest.mark.parametrize("locale", [Locale.RU, Locale.EN])
def test_a008_deleted_kind_is_independent_of_current_existence(
    content: bytes | None, locale: Locale
) -> None:
    prefix = "ru" if locale is Locale.RU else "en"
    path = RepoPath(f"ydb/docs/{prefix}/gone.md")
    reader = LedgerReader({path: content})
    item = discover_changed_pairs(reader, snapshots(), ROOTS, (deleted(path.value),))[0]
    assert item.changes[0].kind is ChangedFileKind.DELETED
    assert item.changes[0].old_path == path
    assert item.changes[0].new_path is None
    side = item.ru if locale is Locale.RU else item.en
    assert side.content is content
    assert side.exists is (content is not None)


@pytest.mark.parametrize("locale", [Locale.RU, Locale.EN])
@pytest.mark.parametrize("state", list(RenameContentState))
def test_a009_rename_preserves_identities_and_content_fact(
    locale: Locale, state: RenameContentState
) -> None:
    root = "ru" if locale is Locale.RU else "en"
    change = renamed(f"ydb/docs/{root}/old.md", f"ydb/docs/{root}/new.md", state)
    classified = classify_changed_file(ROOTS, change)
    assert classified is not None
    assert (classified.locale, classified.old_path, classified.new_path) == (
        locale,
        change.old_path,
        change.new_path,
    )
    assert classified.key == PairKey(RepoPath("new.md"))
    assert classified.previous_key == PairKey(RepoPath("old.md"))
    assert classified.rename_content_state is state


def test_a009_invalid_rename_forms_have_typed_non_echo_failures() -> None:
    with pytest.raises(InvalidChangedFileMetadata) as mixed:
        classify_changed_file(
            ROOTS,
            renamed(
                "ydb/docs/ru/old.txt",
                "ydb/docs/ru/new.md",
                RenameContentState.UNCHANGED,
            ),
        )
    assert mixed.value.reason is InvalidChangeReason.MIXED_MARKDOWN_RENAME
    crossing = renamed(
        "ydb/docs/ru/-old/$x\n*.md",
        "ydb/docs/en/-new/$x\n*.md",
        RenameContentState.CHANGED,
    )
    with pytest.raises(InvalidChangedFileMetadata) as caught:
        classify_changed_file(ROOTS, crossing)
    error = caught.value
    assert error.change is crossing
    assert error.reason is InvalidChangeReason.RENAME_CROSSES_LOCALES
    assert error.args == ("invalid_changed_file_metadata:rename_crosses_locales",)
    for field_value in (crossing.old_path, crossing.new_path):
        assert field_value is not None and field_value.value not in repr(error)
    with pytest.raises(PathOutsideLocaleRoots):
        classify_changed_file(
            ROOTS,
            renamed("outside/old.md", "outside/new.md", RenameContentState.CHANGED),
        )


def test_a007_a009_changed_metadata_shape_matrix_is_exact() -> None:
    p, q = RepoPath("a.md"), RepoPath("b.md")
    invalid = (
        (ChangedFileKind.ADDED, p, q, None),
        (ChangedFileKind.MODIFIED, p, q, None),
        (ChangedFileKind.DELETED, p, q, None),
        (ChangedFileKind.RENAMED, p, p, RenameContentState.CHANGED),
        (ChangedFileKind.RENAMED, p, q, None),
        (ChangedFileKind.ADDED, None, q, RenameContentState.UNCHANGED),
    )
    for args in invalid:
        with pytest.raises(InvariantViolation):
            ChangedFileMetadata(*args)
    with pytest.raises(InvariantViolation):
        ChangedFileMetadata(cast(ChangedFileKind, "added"), None, q, None)


def test_a010_one_mixed_locale_pair_is_read_once_ru_then_en() -> None:
    ru, en = canonical_paths("same.md")
    reader = LedgerReader({ru: b"ru", en: b"en"})
    result = discover_changed_pairs(
        reader,
        snapshots(),
        ROOTS,
        (modified(en.value), modified(ru.value)),
    )
    assert len(result) == 1
    assert result[0].changed_locales == (Locale.RU, Locale.EN)
    assert reader.calls == [(SNAPSHOT, ru), (SNAPSHOT, en)]


def test_a011_matching_renames_group_and_different_origins_conflict() -> None:
    matching = (
        renamed("ydb/docs/ru/old.md", "ydb/docs/ru/new.md", RenameContentState.UNCHANGED),
        renamed("ydb/docs/en/old.md", "ydb/docs/en/new.md", RenameContentState.CHANGED),
    )
    result = discover_changed_pairs(LedgerReader(), snapshots(), ROOTS, matching)
    assert len(result) == 1
    assert result[0].key == PairKey(RepoPath("new.md"))
    assert tuple(c.previous_key for c in result[0].changes) == (
        PairKey(RepoPath("old.md")),
        PairKey(RepoPath("old.md")),
    )
    conflicting = (
        matching[0],
        renamed("ydb/docs/en/other.md", "ydb/docs/en/new.md", RenameContentState.CHANGED),
    )
    expected: tuple[ChangedMarkdownFile, ChangedMarkdownFile] | None = None
    for order in (conflicting, conflicting[::-1]):
        reader = LedgerReader()
        with pytest.raises(ConflictingChangedFileMetadata) as caught:
            discover_changed_pairs(reader, snapshots(), ROOTS, order)
        assert caught.value.reason is MetadataConflictReason.DIFFERENT_RENAME_ORIGIN
        pair = (caught.value.first, caught.value.second)
        expected = expected or pair
        assert pair == expected
        assert reader.calls == []


def test_a012_a013_all_states_and_empty_is_present() -> None:
    contents: dict[RepoPath, bytes | None] = {}
    changes: list[ChangedFileMetadata] = []
    cases = {
        "a-both.md": (b"ru", b"en", PairFileState.BOTH_PRESENT),
        "b-ru.md": (b"", None, PairFileState.RU_ONLY),
        "c-en.md": (None, b"", PairFileState.EN_ONLY),
        "d-none.md": (None, None, PairFileState.BOTH_MISSING),
    }
    for index, (key, (ru_bytes, en_bytes, _)) in enumerate(cases.items()):
        ru, en = canonical_paths(key)
        contents[ru], contents[en] = ru_bytes, en_bytes
        changes.append(modified((ru if index != 2 else en).value))
        if index == 0:
            changes.append(modified(en.value))
    result = discover_changed_pairs(LedgerReader(contents), snapshots(), ROOTS, tuple(changes))
    assert [item.key.relative_path.value for item in result] == list(cases)
    for item, (_, (ru_bytes, en_bytes, state)) in zip(result, cases.items(), strict=True):
        assert item.state is state
        assert item.ru.content is ru_bytes
        assert item.en.content is en_bytes
        assert item.ru.exists is (ru_bytes is not None)
        assert item.en.exists is (en_bytes is not None)
        assert item.is_complete is (state is PairFileState.BOTH_PRESENT)
        assert item.is_one_sided is (state in {PairFileState.RU_ONLY, PairFileState.EN_ONLY})


def test_a014_a015_only_scope_snapshot_is_used_and_mutable_names_are_irrelevant() -> None:
    ru, en = canonical_paths("bound.md")
    reader = LedgerReader({ru: b"ru", en: b"en"})
    resolved = snapshots()
    before = discover_changed_pairs(reader, resolved, ROOTS, (modified(ru.value),))
    reader.head = "new-head"
    reader.worktree = "changed"
    reader.calls.clear()
    after = discover_changed_pairs(reader, resolved, ROOTS, (modified(ru.value),))
    assert after == before
    assert reader.calls == [(resolved.scope_snapshot, ru), (resolved.scope_snapshot, en)]
    assert after[0].ru.snapshot is resolved.scope_snapshot
    assert after[0].en.snapshot is resolved.scope_snapshot
    assert after[0].ru.content == b"ru" and after[0].en.content == b"en"


def test_a014_merged_inventory_uses_distinct_scope_snapshot_for_both_sides() -> None:
    ru, en = canonical_paths("merged.md")
    reader = LedgerReader({ru: b"ru-at-scope", en: b"en-at-scope"})
    resolved = merged_snapshots()

    result = discover_changed_pairs(reader, resolved, ROOTS, (modified(ru.value),))

    assert resolved.pr_snapshot != resolved.scope_snapshot
    assert reader.calls == [
        (MERGED_SCOPE_SNAPSHOT, ru),
        (MERGED_SCOPE_SNAPSHOT, en),
    ]
    assert result[0].ru.snapshot is resolved.scope_snapshot
    assert result[0].en.snapshot is resolved.scope_snapshot


def test_a016_deduplication_sorting_and_read_ledger_are_permutation_stable() -> None:
    base = (modified("ydb/docs/en/z.md"), added("ydb/docs/ru/a.md"))
    expected_result: tuple[LocalePairInventory, ...] | None = None
    expected_calls: list[tuple[SnapshotRef, RepoPath]] | None = None
    for order in itertools.permutations((*base, duplicate(base[0]))):
        reader = LedgerReader()
        result = discover_changed_pairs(reader, snapshots(), ROOTS, order)
        expected_result = expected_result or result
        expected_calls = expected_calls or reader.calls
        assert result == expected_result
        assert reader.calls == expected_calls
    assert [item.key.relative_path.value for item in expected_result or ()] == ["a.md", "z.md"]
    assert [path.value for _, path in expected_calls or []] == [
        "ydb/docs/ru/a.md",
        "ydb/docs/en/a.md",
        "ydb/docs/ru/z.md",
        "ydb/docs/en/z.md",
    ]


def _capture_conflict(
    changes: tuple[ChangedFileMetadata, ...]
) -> tuple[MetadataConflictReason, ChangedMarkdownFile, ChangedMarkdownFile]:
    reader = LedgerReader()
    with pytest.raises(ConflictingChangedFileMetadata) as caught:
        discover_changed_pairs(reader, snapshots(), ROOTS, changes)
    assert reader.calls == []
    return caught.value.reason, caught.value.first, caught.value.second


def test_a017_all_conflicts_are_reachable_canonical_and_globally_prioritized() -> None:
    same = (added("ydb/docs/ru/a.md"), modified("ydb/docs/ru/a.md"))
    reused = (
        renamed("ydb/docs/ru/old.md", "ydb/docs/ru/new.md", RenameContentState.CHANGED),
        modified("ydb/docs/ru/old.md"),
    )
    origins = (
        renamed("ydb/docs/ru/x.md", "ydb/docs/ru/z.md", RenameContentState.CHANGED),
        renamed("ydb/docs/en/y.md", "ydb/docs/en/z.md", RenameContentState.CHANGED),
    )
    for records, reason in (
        (same, MetadataConflictReason.SAME_LOCALE_PAIR),
        (reused, MetadataConflictReason.REUSED_PHYSICAL_PATH),
        (origins, MetadataConflictReason.DIFFERENT_RENAME_ORIGIN),
    ):
        assert _capture_conflict(records) == _capture_conflict(records[::-1])
        assert _capture_conflict(records)[0] is reason
    combined = (*origins, *reused, *same)
    expected = _capture_conflict(combined)
    assert expected[0] is MetadataConflictReason.SAME_LOCALE_PAIR
    for order in itertools.permutations(combined):
        assert _capture_conflict(order) == expected
    same_value = duplicate(same[0])
    assert len(discover_changed_pairs(LedgerReader(), snapshots(), ROOTS, (same[0], same_value))) == 1


def test_a017_conflict_error_is_typed_and_does_not_echo_records() -> None:
    first = cast(ChangedMarkdownFile, classify_changed_file(ROOTS, added("ydb/docs/ru/$x\n*.md")))
    second = cast(ChangedMarkdownFile, classify_changed_file(ROOTS, modified("ydb/docs/ru/$x\n*.md")))
    error = ConflictingChangedFileMetadata(first, second, MetadataConflictReason.SAME_LOCALE_PAIR)
    assert (error.first, error.second, error.reason) == (
        first,
        second,
        MetadataConflictReason.SAME_LOCALE_PAIR,
    )
    assert error.args == ("conflicting_changed_file_metadata:same_locale_pair",)
    assert first.key.relative_path.value not in repr(error)


def test_a018_reader_failure_propagates_without_retry_or_later_reads() -> None:
    sentinel = RuntimeError("sentinel")

    class FailingReader(LedgerReader):
        def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
            self.calls.append((snapshot, path))
            if path == RepoPath("ydb/docs/en/a.md"):
                raise sentinel
            return b"ok"

    reader = FailingReader()
    with pytest.raises(RuntimeError) as caught:
        discover_changed_pairs(
            reader,
            snapshots(),
            ROOTS,
            (modified("ydb/docs/ru/a.md"), modified("ydb/docs/ru/z.md")),
        )
    assert caught.value is sentinel
    assert [path.value for _, path in reader.calls] == ["ydb/docs/ru/a.md", "ydb/docs/en/a.md"]


def _a019_record_contract() -> tuple[
    tuple[type[object], tuple[str, ...], object, str, object], ...
]:
    key = PairKey(RepoPath("a.md"))
    ru, en = canonical_paths("a.md")
    change = ChangedMarkdownFile(
        ROOTS,
        ChangedFileKind.MODIFIED,
        Locale.RU,
        key,
        ru,
        ru,
        None,
        None,
    )
    side_ru = SnapshotLocaleFile(ROOTS, Locale.RU, key, ru, SNAPSHOT, b"")
    side_en = SnapshotLocaleFile(ROOTS, Locale.EN, key, en, SNAPSHOT, None)
    return (
        (LocaleRoots, ("ru", "en"), ROOTS, "ru", RepoPath("other/ru")),
        (PairKey, ("relative_path",), key, "relative_path", RepoPath("b.md")),
        (
            LocalizedMarkdownPath,
            ("roots", "locale", "path", "key"),
            LocalizedMarkdownPath(ROOTS, Locale.RU, ru, key),
            "path",
            RepoPath("ydb/docs/ru/b.md"),
        ),
        (
            ChangedFileMetadata,
            ("kind", "old_path", "new_path", "rename_content_state"),
            ChangedFileMetadata(ChangedFileKind.MODIFIED, ru, ru, None),
            "old_path",
            RepoPath("ydb/docs/ru/b.md"),
        ),
        (
            ChangedMarkdownFile,
            (
                "roots",
                "kind",
                "locale",
                "key",
                "old_path",
                "new_path",
                "previous_key",
                "rename_content_state",
            ),
            change,
            "new_path",
            RepoPath("ydb/docs/ru/b.md"),
        ),
        (
            SnapshotLocaleFile,
            ("roots", "locale", "key", "path", "snapshot", "content"),
            side_ru,
            "content",
            b"changed",
        ),
        (
            LocalePairInventory,
            ("roots", "key", "ru", "en", "changes", "state"),
            LocalePairInventory(
                ROOTS,
                key,
                side_ru,
                side_en,
                (change,),
                PairFileState.RU_ONLY,
            ),
            "changes",
            (),
        ),
    )


class _BytesSubclass(bytes):
    pass


class _RepoPathSubclass(RepoPath):
    def __eq__(self, other: object) -> bool:
        return isinstance(other, RepoPath) and self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)


class _LocaleRootsSubclass(LocaleRoots):
    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, LocaleRoots)
            and self.ru == other.ru
            and self.en == other.en
        )

    def __hash__(self) -> int:
        return hash((self.ru, self.en))


class _PairKeySubclass(PairKey):
    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, PairKey)
            and self.relative_path == other.relative_path
        )

    def __hash__(self) -> int:
        return hash(self.relative_path)


class _ChangedFileMetadataSubclass(ChangedFileMetadata):
    pass


class _ChangedMarkdownFileSubclass(ChangedMarkdownFile):
    pass


class _SnapshotRefSubclass(SnapshotRef):
    pass


class _SnapshotLocaleFileSubclass(SnapshotLocaleFile):
    pass


class _TupleSubclass(tuple[ChangedMarkdownFile, ...]):
    pass


def _a019_strict_subclass_fields() -> tuple[
    tuple[str, str, Callable[[], object]], ...
]:
    key = PairKey(RepoPath("a.md"))
    ru, en = canonical_paths("a.md")
    old_ru = RepoPath("ydb/docs/ru/old.md")
    equal_ru = _RepoPathSubclass(ru.value)
    equal_en = _RepoPathSubclass(en.value)
    roots_subclass = _LocaleRootsSubclass(ROOTS.ru, ROOTS.en)
    key_subclass = _PairKeySubclass(RepoPath("a.md"))
    previous_key_subclass = _PairKeySubclass(RepoPath("old.md"))
    change = ChangedMarkdownFile(
        ROOTS,
        ChangedFileKind.MODIFIED,
        Locale.RU,
        key,
        ru,
        ru,
        None,
        None,
    )
    change_subclass = _ChangedMarkdownFileSubclass(
        ROOTS,
        ChangedFileKind.MODIFIED,
        Locale.RU,
        key,
        ru,
        ru,
        None,
        None,
    )
    side_ru = SnapshotLocaleFile(ROOTS, Locale.RU, key, ru, SNAPSHOT, b"")
    side_en = SnapshotLocaleFile(ROOTS, Locale.EN, key, en, SNAPSHOT, None)
    side_ru_subclass = _SnapshotLocaleFileSubclass(
        ROOTS, Locale.RU, key, ru, SNAPSHOT, b""
    )
    side_en_subclass = _SnapshotLocaleFileSubclass(
        ROOTS, Locale.EN, key, en, SNAPSHOT, None
    )
    snapshot_subclass = _SnapshotRefSubclass(
        SNAPSHOT.repository, SNAPSHOT.commit_sha
    )
    metadata_subclass = _ChangedFileMetadataSubclass(
        ChangedFileKind.MODIFIED, ru, ru, None
    )
    return (
        ("LocaleRoots", "ru", lambda: LocaleRoots(equal_ru, ROOTS.en)),
        ("LocaleRoots", "en", lambda: LocaleRoots(ROOTS.ru, equal_en)),
        ("PairKey", "relative_path", lambda: PairKey(_RepoPathSubclass("a.md"))),
        (
            "LocalizedMarkdownPath",
            "roots",
            lambda: LocalizedMarkdownPath(roots_subclass, Locale.RU, ru, key),
        ),
        (
            "LocalizedMarkdownPath",
            "path",
            lambda: LocalizedMarkdownPath(ROOTS, Locale.RU, equal_ru, key),
        ),
        (
            "LocalizedMarkdownPath",
            "key",
            lambda: LocalizedMarkdownPath(ROOTS, Locale.RU, ru, key_subclass),
        ),
        (
            "ChangedFileMetadata",
            "old_path",
            lambda: ChangedFileMetadata(
                ChangedFileKind.MODIFIED, equal_ru, ru, None
            ),
        ),
        (
            "ChangedFileMetadata",
            "new_path",
            lambda: ChangedFileMetadata(
                ChangedFileKind.MODIFIED, ru, equal_ru, None
            ),
        ),
        (
            "ChangedMarkdownFile",
            "roots",
            lambda: ChangedMarkdownFile(
                roots_subclass,
                ChangedFileKind.MODIFIED,
                Locale.RU,
                key,
                ru,
                ru,
                None,
                None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "key",
            lambda: ChangedMarkdownFile(
                ROOTS,
                ChangedFileKind.MODIFIED,
                Locale.RU,
                key_subclass,
                ru,
                ru,
                None,
                None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "old_path",
            lambda: ChangedMarkdownFile(
                ROOTS,
                ChangedFileKind.MODIFIED,
                Locale.RU,
                key,
                equal_ru,
                ru,
                None,
                None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "new_path",
            lambda: ChangedMarkdownFile(
                ROOTS,
                ChangedFileKind.MODIFIED,
                Locale.RU,
                key,
                ru,
                equal_ru,
                None,
                None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "previous_key",
            lambda: ChangedMarkdownFile(
                ROOTS,
                ChangedFileKind.RENAMED,
                Locale.RU,
                key,
                old_ru,
                ru,
                previous_key_subclass,
                RenameContentState.CHANGED,
            ),
        ),
        (
            "SnapshotLocaleFile",
            "roots",
            lambda: SnapshotLocaleFile(
                roots_subclass, Locale.RU, key, ru, SNAPSHOT, b""
            ),
        ),
        (
            "SnapshotLocaleFile",
            "key",
            lambda: SnapshotLocaleFile(
                ROOTS, Locale.RU, key_subclass, ru, SNAPSHOT, b""
            ),
        ),
        (
            "SnapshotLocaleFile",
            "path",
            lambda: SnapshotLocaleFile(
                ROOTS, Locale.RU, key, equal_ru, SNAPSHOT, b""
            ),
        ),
        (
            "SnapshotLocaleFile",
            "snapshot",
            lambda: SnapshotLocaleFile(
                ROOTS, Locale.RU, key, ru, snapshot_subclass, b""
            ),
        ),
        (
            "LocalePairInventory",
            "roots",
            lambda: LocalePairInventory(
                roots_subclass,
                key,
                side_ru,
                side_en,
                (change,),
                PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "key",
            lambda: LocalePairInventory(
                ROOTS,
                key_subclass,
                side_ru,
                side_en,
                (change,),
                PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "ru",
            lambda: LocalePairInventory(
                ROOTS,
                key,
                side_ru_subclass,
                side_en,
                (change,),
                PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "en",
            lambda: LocalePairInventory(
                ROOTS,
                key,
                side_ru,
                side_en_subclass,
                (change,),
                PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "changes",
            lambda: LocalePairInventory(
                ROOTS,
                key,
                side_ru,
                side_en,
                _TupleSubclass((change,)),
                PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "changes",
            lambda: LocalePairInventory(
                ROOTS,
                key,
                side_ru,
                side_en,
                (change_subclass,),
                PairFileState.RU_ONLY,
            ),
        ),
        (
            "NonMarkdownPath",
            "path",
            lambda: NonMarkdownPath(_RepoPathSubclass("a.txt")),
        ),
        (
            "PathOutsideLocaleRoots",
            "path",
            lambda: PathOutsideLocaleRoots(_RepoPathSubclass("other/a.md")),
        ),
        (
            "InvalidChangedFileMetadata",
            "change",
            lambda: InvalidChangedFileMetadata(
                metadata_subclass, InvalidChangeReason.MIXED_MARKDOWN_RENAME
            ),
        ),
        (
            "ConflictingChangedFileMetadata",
            "first",
            lambda: ConflictingChangedFileMetadata(
                change_subclass,
                change,
                MetadataConflictReason.SAME_LOCALE_PAIR,
            ),
        ),
        (
            "ConflictingChangedFileMetadata",
            "second",
            lambda: ConflictingChangedFileMetadata(
                change,
                change_subclass,
                MetadataConflictReason.SAME_LOCALE_PAIR,
            ),
        ),
    )


def _a019_wrong_record_fields() -> tuple[
    tuple[str, str, Callable[[], object]], ...
]:
    key = PairKey(RepoPath("a.md"))
    ru, en = canonical_paths("a.md")
    change = ChangedMarkdownFile(
        ROOTS,
        ChangedFileKind.MODIFIED,
        Locale.RU,
        key,
        ru,
        ru,
        None,
        None,
    )
    side_ru = SnapshotLocaleFile(ROOTS, Locale.RU, key, ru, SNAPSHOT, b"")
    side_en = SnapshotLocaleFile(ROOTS, Locale.EN, key, en, SNAPSHOT, None)
    wrong = object()
    return (
        ("LocaleRoots", "ru", lambda: LocaleRoots(cast(RepoPath, wrong), ROOTS.en)),
        ("LocaleRoots", "en", lambda: LocaleRoots(ROOTS.ru, cast(RepoPath, wrong))),
        ("PairKey", "relative_path", lambda: PairKey(cast(RepoPath, wrong))),
        (
            "LocalizedMarkdownPath",
            "roots",
            lambda: LocalizedMarkdownPath(cast(LocaleRoots, wrong), Locale.RU, ru, key),
        ),
        (
            "LocalizedMarkdownPath",
            "locale",
            lambda: LocalizedMarkdownPath(ROOTS, cast(Locale, wrong), ru, key),
        ),
        (
            "LocalizedMarkdownPath",
            "path",
            lambda: LocalizedMarkdownPath(ROOTS, Locale.RU, cast(RepoPath, wrong), key),
        ),
        (
            "LocalizedMarkdownPath",
            "key",
            lambda: LocalizedMarkdownPath(ROOTS, Locale.RU, ru, cast(PairKey, wrong)),
        ),
        (
            "ChangedFileMetadata",
            "kind",
            lambda: ChangedFileMetadata(cast(ChangedFileKind, wrong), ru, ru, None),
        ),
        (
            "ChangedFileMetadata",
            "old_path",
            lambda: ChangedFileMetadata(
                ChangedFileKind.MODIFIED, cast(RepoPath, wrong), ru, None
            ),
        ),
        (
            "ChangedFileMetadata",
            "new_path",
            lambda: ChangedFileMetadata(
                ChangedFileKind.MODIFIED, ru, cast(RepoPath, wrong), None
            ),
        ),
        (
            "ChangedFileMetadata",
            "rename_content_state",
            lambda: ChangedFileMetadata(
                ChangedFileKind.MODIFIED,
                ru,
                ru,
                cast(RenameContentState, wrong),
            ),
        ),
        (
            "ChangedMarkdownFile",
            "roots",
            lambda: ChangedMarkdownFile(
                cast(LocaleRoots, wrong), ChangedFileKind.MODIFIED, Locale.RU,
                key, ru, ru, None, None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "kind",
            lambda: ChangedMarkdownFile(
                ROOTS, cast(ChangedFileKind, wrong), Locale.RU,
                key, ru, ru, None, None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "locale",
            lambda: ChangedMarkdownFile(
                ROOTS, ChangedFileKind.MODIFIED, cast(Locale, wrong),
                key, ru, ru, None, None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "key",
            lambda: ChangedMarkdownFile(
                ROOTS, ChangedFileKind.MODIFIED, Locale.RU,
                cast(PairKey, wrong), ru, ru, None, None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "old_path",
            lambda: ChangedMarkdownFile(
                ROOTS, ChangedFileKind.MODIFIED, Locale.RU,
                key, cast(RepoPath, wrong), ru, None, None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "new_path",
            lambda: ChangedMarkdownFile(
                ROOTS, ChangedFileKind.MODIFIED, Locale.RU,
                key, ru, cast(RepoPath, wrong), None, None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "previous_key",
            lambda: ChangedMarkdownFile(
                ROOTS, ChangedFileKind.MODIFIED, Locale.RU,
                key, ru, ru, cast(PairKey, wrong), None,
            ),
        ),
        (
            "ChangedMarkdownFile",
            "rename_content_state",
            lambda: ChangedMarkdownFile(
                ROOTS, ChangedFileKind.MODIFIED, Locale.RU,
                key, ru, ru, None, cast(RenameContentState, wrong),
            ),
        ),
        (
            "SnapshotLocaleFile",
            "roots",
            lambda: SnapshotLocaleFile(
                cast(LocaleRoots, wrong), Locale.RU, key, ru, SNAPSHOT, b""
            ),
        ),
        (
            "SnapshotLocaleFile",
            "locale",
            lambda: SnapshotLocaleFile(
                ROOTS, cast(Locale, wrong), key, ru, SNAPSHOT, b""
            ),
        ),
        (
            "SnapshotLocaleFile",
            "key",
            lambda: SnapshotLocaleFile(
                ROOTS, Locale.RU, cast(PairKey, wrong), ru, SNAPSHOT, b""
            ),
        ),
        (
            "SnapshotLocaleFile",
            "path",
            lambda: SnapshotLocaleFile(
                ROOTS, Locale.RU, key, cast(RepoPath, wrong), SNAPSHOT, b""
            ),
        ),
        (
            "SnapshotLocaleFile",
            "snapshot",
            lambda: SnapshotLocaleFile(
                ROOTS, Locale.RU, key, ru, cast(SnapshotRef, wrong), b""
            ),
        ),
        (
            "SnapshotLocaleFile",
            "content",
            lambda: SnapshotLocaleFile(
                ROOTS, Locale.RU, key, ru, SNAPSHOT, cast(bytes, bytearray())
            ),
        ),
        (
            "SnapshotLocaleFile",
            "content",
            lambda: SnapshotLocaleFile(
                ROOTS, Locale.RU, key, ru, SNAPSHOT, _BytesSubclass()
            ),
        ),
        (
            "LocalePairInventory",
            "roots",
            lambda: LocalePairInventory(
                cast(LocaleRoots, wrong), key, side_ru, side_en,
                (change,), PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "key",
            lambda: LocalePairInventory(
                ROOTS, cast(PairKey, wrong), side_ru, side_en,
                (change,), PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "ru",
            lambda: LocalePairInventory(
                ROOTS, key, cast(SnapshotLocaleFile, wrong), side_en,
                (change,), PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "en",
            lambda: LocalePairInventory(
                ROOTS, key, side_ru, cast(SnapshotLocaleFile, wrong),
                (change,), PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "changes",
            lambda: LocalePairInventory(
                ROOTS, key, side_ru, side_en,
                cast(tuple[ChangedMarkdownFile, ...], [change]), PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "changes",
            lambda: LocalePairInventory(
                ROOTS, key, side_ru, side_en,
                cast(tuple[ChangedMarkdownFile, ...], (wrong,)), PairFileState.RU_ONLY,
            ),
        ),
        (
            "LocalePairInventory",
            "state",
            lambda: LocalePairInventory(
                ROOTS, key, side_ru, side_en,
                (change,), cast(PairFileState, wrong),
            ),
        ),
    )


def _a019_wrong_exception_fields() -> tuple[
    tuple[str, str, Callable[[], object]], ...
]:
    metadata = modified("ydb/docs/ru/a.md")
    change = ChangedMarkdownFile(
        ROOTS,
        ChangedFileKind.MODIFIED,
        Locale.RU,
        PairKey(RepoPath("a.md")),
        RepoPath("ydb/docs/ru/a.md"),
        RepoPath("ydb/docs/ru/a.md"),
        None,
        None,
    )
    wrong = object()
    return (
        ("NonMarkdownPath", "path", lambda: NonMarkdownPath(cast(RepoPath, wrong))),
        (
            "PathOutsideLocaleRoots",
            "path",
            lambda: PathOutsideLocaleRoots(cast(RepoPath, wrong)),
        ),
        (
            "InvalidChangedFileMetadata",
            "change",
            lambda: InvalidChangedFileMetadata(
                cast(ChangedFileMetadata, wrong),
                InvalidChangeReason.MIXED_MARKDOWN_RENAME,
            ),
        ),
        (
            "InvalidChangedFileMetadata",
            "reason",
            lambda: InvalidChangedFileMetadata(
                metadata, cast(InvalidChangeReason, wrong)
            ),
        ),
        (
            "ConflictingChangedFileMetadata",
            "first",
            lambda: ConflictingChangedFileMetadata(
                cast(ChangedMarkdownFile, wrong),
                change,
                MetadataConflictReason.SAME_LOCALE_PAIR,
            ),
        ),
        (
            "ConflictingChangedFileMetadata",
            "second",
            lambda: ConflictingChangedFileMetadata(
                change,
                cast(ChangedMarkdownFile, wrong),
                MetadataConflictReason.SAME_LOCALE_PAIR,
            ),
        ),
        (
            "ConflictingChangedFileMetadata",
            "reason",
            lambda: ConflictingChangedFileMetadata(
                change,
                change,
                cast(MetadataConflictReason, wrong),
            ),
        ),
    )


def test_a019_exact_public_surface_records_enums_signatures_and_freezing() -> None:
    import ydbdoc_review_ng.locales as module

    assert module.__all__ == (
        "ChangedFileKind", "ChangedFileMetadata", "ChangedMarkdownFile",
        "ConflictingChangedFileMetadata", "InvalidChangeReason",
        "InvalidChangedFileMetadata", "LocalePairInventory", "LocalePathError",
        "LocaleRoots", "LocalizedMarkdownPath", "MetadataConflictReason",
        "NonMarkdownPath", "PairDiscoveryError", "PairFileState", "PairKey",
        "PathOutsideLocaleRoots", "RenameContentState", "SnapshotLocaleFile",
        "classify_changed_file", "discover_changed_pairs", "is_markdown_path",
        "locate_markdown_path", "paired_markdown_path",
    )
    assert [member.value for member in ChangedFileKind] == ["added", "modified", "deleted", "renamed"]
    assert [member.value for member in RenameContentState] == ["unchanged", "changed"]
    assert [member.value for member in PairFileState] == ["both_present", "ru_only", "en_only", "both_missing"]
    assert [member.value for member in InvalidChangeReason] == ["mixed_markdown_rename", "rename_crosses_locales"]
    assert [member.value for member in MetadataConflictReason] == ["same_locale_pair", "reused_physical_path", "different_rename_origin"]
    for enum_type in (ChangedFileKind, RenameContentState, PairFileState, InvalidChangeReason, MetadataConflictReason):
        assert issubclass(enum_type, str) and issubclass(enum_type, Enum)
    record_contract = _a019_record_contract()
    assert tuple(record_type.__name__ for record_type, *_ in record_contract) == (
        "LocaleRoots",
        "PairKey",
        "LocalizedMarkdownPath",
        "ChangedFileMetadata",
        "ChangedMarkdownFile",
        "SnapshotLocaleFile",
        "LocalePairInventory",
    )
    assert {
        value.__name__
        for value in vars(module).values()
        if inspect.isclass(value)
        and value.__module__ == module.__name__
        and hasattr(value, "__dataclass_fields__")
    } == {record_type.__name__ for record_type, *_ in record_contract}
    for record_type, field_names, instance, assignment_field, replacement in record_contract:
        assert tuple(field.name for field in fields(record_type)) == field_names
        with pytest.raises(FrozenInstanceError):
            setattr(instance, assignment_field, replacement)
        assert not hasattr(instance, "__dict__")
    assert all(param.kind is inspect.Parameter.POSITIONAL_ONLY for param in inspect.signature(is_markdown_path).parameters.values())
    assert all(param.kind is inspect.Parameter.POSITIONAL_ONLY for param in inspect.signature(locate_markdown_path).parameters.values())
    assert all(param.kind is inspect.Parameter.POSITIONAL_ONLY for param in inspect.signature(paired_markdown_path).parameters.values())
    assert all(param.kind is inspect.Parameter.POSITIONAL_ONLY for param in inspect.signature(classify_changed_file).parameters.values())
    assert all(param.kind is inspect.Parameter.POSITIONAL_ONLY for param in inspect.signature(discover_changed_pairs).parameters.values())
    assert issubclass(NonMarkdownPath, LocalePathError)
    assert issubclass(InvalidChangedFileMetadata, PairDiscoveryError)


def test_a019_direct_record_and_exception_construction_rejects_wrong_exact_types() -> None:
    for type_name, field_name, construct in (
        *_a019_wrong_record_fields(),
        *_a019_wrong_exception_fields(),
        *_a019_strict_subclass_fields(),
    ):
        with pytest.raises(
            InvariantViolation,
            match=rf"^{type_name}\.{field_name}: expected ",
        ):
            construct()


def test_a020_t004_is_not_added_to_frozen_wire_schema() -> None:
    assert DOMAIN_SCHEMA_VERSION == 1
    records: tuple[object, ...] = (
        ROOTS,
        PairKey(RepoPath("a.md")),
        added("ydb/docs/ru/a.md"),
        cast(ChangedMarkdownFile, classify_changed_file(ROOTS, added("ydb/docs/ru/a.md"))),
    )
    for record in records:
        with pytest.raises(UnknownDomainType):
            to_wire(record)  # type: ignore[arg-type]


def test_a021_real_pr_fixture_provenance_and_hashes_are_offline() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "pairs"
    expected = {
        "auth_config.slice": (
            397,
            "b4e37962224c79ce22b02deb9571df0fcd99974eba50ded4a897e34022dcf770",
        ),
        "authentication.slice": (
            694,
            "d13d910ccaefec0eab57c927317caa5f863081455a6a2371192a57443548a80b",
        ),
    }
    stored = tuple((fixture_root / name).read_bytes() for name in expected)
    assert all(stored)
    assert len(set(stored)) == len(stored)
    for (name, (size, digest)), payload in zip(expected.items(), stored, strict=True):
        assert len(payload) == size
        assert hashlib.sha256(payload).hexdigest() == digest
    provenance = (fixture_root / "PROVENANCE.md").read_text()
    assert "ydb-platform/ydb#51079" in provenance
    assert SNAPSHOT.commit_sha.value in provenance
    assert "0c14e825339608953089b25467b0e759b6edfe4c052ba94212c0a053eca9cde3" in provenance
    assert "fa33f8aba3cedeb042535c976a7d20a0649ee243153281c35e8ddf2dc35415e3" in provenance
    assert "[0, 397)" in provenance
    assert "[0, 694)" in provenance
    assert all(digest in provenance for _, digest in expected.values())
    assert "generated" in provenance.lower()
    assert "per-file status" in provenance

    real_paths = (
        RepoPath("ydb/docs/ru/core/reference/configuration/auth_config.md"),
        RepoPath("ydb/docs/ru/core/security/authentication.md"),
    )
    reader = LedgerReader(dict(zip(real_paths, stored, strict=True)))
    inventories = discover_changed_pairs(
        reader,
        snapshots(),
        ROOTS,
        tuple(modified(path.value) for path in real_paths),
    )
    assert [item.state for item in inventories] == [PairFileState.RU_ONLY, PairFileState.RU_ONLY]
    assert tuple(item.ru.content for item in inventories) == stored
    assert all(item.ru.exists for item in inventories)


def test_a002_a019_a022_static_import_and_future_policy_audit() -> None:
    source_path = Path(__file__).parents[2] / "src" / "ydbdoc_review_ng" / "locales.py"
    source = source_path.read_text()
    assert all(token not in source for token in ("YDB_DOC_ROOT", '"docs/ru"', '"docs/en"', '"ydb/docs/ru"', '"ydb/docs/en"'))
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imports <= {
        "__future__", "dataclasses", "enum", "itertools",
        "ydbdoc_review_ng.domain", "ydbdoc_review_ng.errors",
        "ydbdoc_review_ng.ports", "ydbdoc_review_ng.repository",
    }
    forbidden = (
        "direction_result", "source_locale", "target_locale", "model_client",
        "dependency", "redirect", "tombstone", "publication", "worktree",
        "github", "subprocess", "pathlib", "open(", "os.environ",
    )
    lowered = source.lower()
    assert all(word not in lowered for word in forbidden)
    for dependency in ("domain.py", "ports.py", "repository.py"):
        dependency_tree = ast.parse((source_path.parent / dependency).read_text())
        dependency_imports = {
            node.module or ""
            for node in ast.walk(dependency_tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "ydbdoc_review_ng.locales" not in dependency_imports
