from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path

import pytest

from ydbdoc_review_ng import dependencies
from ydbdoc_review_ng.direction import (
    Direction,
    DirectionPairDecision,
    DirectionPairVerdict,
    DirectionSelectionResult,
    DirectionSelectionState,
)
from ydbdoc_review_ng.domain import GitSha, Locale, RepoPath, RepositoryId, SnapshotRef, to_wire
from ydbdoc_review_ng.errors import InvariantViolation, UnknownDomainType
from ydbdoc_review_ng.locales import (
    ChangedFileKind,
    ChangedMarkdownFile,
    LocalePairInventory,
    LocaleRoots,
    PairFileState,
    PairKey,
    RenameContentState,
    SnapshotLocaleFile,
)
from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
from ydbdoc_review_ng.scope import (
    FileOperation,
    InvalidScopeInput,
    ScopeInputReason,
    ScopeOrigin,
    ScopeSelectionState,
    build_potential_scopes,
    freeze_scope_manifest,
)


def test_public_dependency_contract_inventory() -> None:
    expected = {
        "DependencyInputReason",
        "DependencyResolutionState",
        "DependencyLink",
        "RedirectEntry",
        "RedirectCatalog",
        "ResolvedDependency",
        "DependencySource",
        "DependencyError",
        "InvalidDependencyInput",
        "RedirectCycleDetected",
        "resolve_redirect",
    }
    assert set(dependencies.__all__) == expected
    assert len(tuple(dependencies.DependencyInputReason)) == 9
    assert len(tuple(dependencies.DependencyResolutionState)) == 4
    assert all(
        issubclass(item, str) and issubclass(item, Enum)
        for item in (
            dependencies.DependencyInputReason,
            dependencies.DependencyResolutionState,
        )
    )


@pytest.mark.parametrize(
    ("record", "field_names"),
    [
        (dependencies.DependencyLink, ("source_path", "destination_source_path")),
        (dependencies.RedirectEntry, ("from_path", "to_path")),
        (dependencies.RedirectCatalog, ("snapshot", "roots", "entries")),
        (dependencies.ResolvedDependency, ("link", "source_path", "target_path", "state")),
    ],
)
def test_dependency_records_are_frozen_slotted_with_exact_field_order(
    record: type[object], field_names: tuple[str, ...]
) -> None:
    assert is_dataclass(record)
    assert tuple(item.name for item in fields(record)) == field_names
    assert "__dict__" not in record.__slots__


def test_dependency_signatures_are_positional_only() -> None:
    assert str(inspect.signature(dependencies.DependencySource.links)) == (
        "(self, snapshot: 'SnapshotRef', source_path: 'RepoPath', source_content: 'bytes', /) "
        "-> 'tuple[DependencyLink, ...]'"
    )


def _snapshot() -> SnapshotRef:
    return SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


def _roots() -> LocaleRoots:
    return LocaleRoots(RepoPath("ydb/docs/ru"), RepoPath("ydb/docs/en"))


def test_redirect_catalog_canonicalizes_and_resolves_terminal_chain() -> None:
    catalog = dependencies.RedirectCatalog(
        _snapshot(),
        _roots(),
        (
            dependencies.RedirectEntry(RepoPath("ydb/docs/ru/b.md"), RepoPath("ydb/docs/ru/c.md")),
            dependencies.RedirectEntry(RepoPath("ydb/docs/ru/a.md"), RepoPath("ydb/docs/ru/b.md")),
        ),
    )
    assert tuple(item.from_path.value for item in catalog.entries) == (
        "ydb/docs/ru/a.md",
        "ydb/docs/ru/b.md",
    )
    assert dependencies.resolve_redirect(
        catalog, Locale.RU, RepoPath("ydb/docs/ru/a.md")
    ) == RepoPath("ydb/docs/ru/c.md")


def test_redirect_cycle_is_canonical_and_closed() -> None:
    catalog = dependencies.RedirectCatalog(
        _snapshot(),
        _roots(),
        (
            dependencies.RedirectEntry(RepoPath("ydb/docs/ru/z.md"), RepoPath("ydb/docs/ru/b.md")),
            dependencies.RedirectEntry(RepoPath("ydb/docs/ru/b.md"), RepoPath("ydb/docs/ru/z.md")),
        ),
    )
    with pytest.raises(dependencies.RedirectCycleDetected) as caught:
        dependencies.resolve_redirect(catalog, Locale.RU, RepoPath("ydb/docs/ru/z.md"))
    assert caught.value.args == ("redirect_cycle_detected",)
    assert caught.value.locale is Locale.RU
    assert tuple(path.value for path in caught.value.paths) == (
        "ydb/docs/ru/b.md",
        "ydb/docs/ru/z.md",
        "ydb/docs/ru/b.md",
    )


@pytest.mark.parametrize(
    ("entries", "reason", "path"),
    [
        (
            ("ydb/docs/ru/a.txt", "ydb/docs/ru/b.md"),
            dependencies.DependencyInputReason.NON_MARKDOWN_REDIRECT,
            "ydb/docs/ru/a.txt",
        ),
        (
            ("outside/a.md", "outside/b.md"),
            dependencies.DependencyInputReason.PATH_OUTSIDE_SOURCE_LOCALE,
            "outside/a.md",
        ),
        (
            ("ydb/docs/ru/a.md", "ydb/docs/en/a.md"),
            dependencies.DependencyInputReason.REDIRECT_CROSSES_LOCALES,
            "ydb/docs/ru/a.md",
        ),
        (
            ("ydb/docs/ru/a.md", "ydb/docs/ru/a.md"),
            dependencies.DependencyInputReason.REDIRECT_SELF_LOOP,
            "ydb/docs/ru/a.md",
        ),
    ],
)
def test_redirect_catalog_validation(entries, reason, path) -> None:
    with pytest.raises(dependencies.InvalidDependencyInput) as caught:
        dependencies.RedirectCatalog(
            _snapshot(),
            _roots(),
            (dependencies.RedirectEntry(RepoPath(entries[0]), RepoPath(entries[1])),),
        )
    assert caught.value.reason is reason
    assert caught.value.path == RepoPath(path)
    assert str(caught.value) == f"invalid_dependency_input:{reason.value}"
    assert path not in str(caught.value)


@pytest.mark.parametrize(
    ("name", "size", "digest"),
    [
        (
            "auth_config.slice",
            397,
            "b4e37962224c79ce22b02deb9571df0fcd99974eba50ded4a897e34022dcf770",
        ),
        (
            "authentication.slice",
            694,
            "d13d910ccaefec0eab57c927317caa5f863081455a6a2371192a57443548a80b",
        ),
    ],
)
def test_pr51079_fixture_bytes_are_exact(name: str, size: int, digest: str) -> None:
    # Synthetic topology over attributed document bytes.
    payload = (Path(__file__).parents[1] / "fixtures" / "scope" / "pr51079" / name).read_bytes()
    assert len(payload) == size
    assert hashlib.sha256(payload).hexdigest() == digest


def _inventory(key_value: str, ru: bytes | None, en: bytes | None) -> LocalePairInventory:
    roots = _roots()
    snapshot = _snapshot()
    key = PairKey(RepoPath(key_value))
    ru_path = RepoPath(f"{roots.ru.value}/{key_value}")
    en_path = RepoPath(f"{roots.en.value}/{key_value}")
    change = ChangedMarkdownFile(
        roots,
        ChangedFileKind.ADDED,
        Locale.RU,
        key,
        None,
        ru_path,
        None,
        None,
    )
    state = (
        PairFileState.BOTH_PRESENT
        if ru is not None and en is not None
        else PairFileState.RU_ONLY
        if ru is not None
        else PairFileState.EN_ONLY
        if en is not None
        else PairFileState.BOTH_MISSING
    )
    return LocalePairInventory(
        roots,
        key,
        SnapshotLocaleFile(roots, Locale.RU, key, ru_path, snapshot, ru),
        SnapshotLocaleFile(roots, Locale.EN, key, en_path, snapshot, en),
        (change,),
        state,
    )


def _snapshots() -> ResolvedRepositorySnapshots:
    snapshot = _snapshot()
    return ResolvedRepositorySnapshots(
        PullRequestState.OPEN,
        BaseBranch("main"),
        snapshot,
        snapshot,
        snapshot,
        snapshot,
        snapshot,
        None,
        None,
    )


def _merged_snapshots() -> ResolvedRepositorySnapshots:
    scope_snapshot = _snapshot()
    merged_pr = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("b" * 40))
    return ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        merged_pr,
        scope_snapshot,
        scope_snapshot,
        scope_snapshot,
        scope_snapshot,
        scope_snapshot,
        merged_pr,
    )


class _Reader:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def read_bytes(self, snapshot, path, /):
        self.calls.append((snapshot, path))
        return self.values.get(path)


class _Scanner:
    def __init__(self, links):
        self.by_path = links
        self.calls = []

    def links(self, snapshot, source_path, source_content, /):
        self.calls.append((snapshot, source_path, source_content))
        return self.by_path.get(source_path, ())


class _Preflight:
    def __init__(self):
        self.calls = []

    def check(self, request, /):
        self.calls.append(request)


def test_empty_job_returns_without_any_collaborator_call() -> None:
    reader = _Reader({})
    scanner = _Scanner({})
    preflight = _Preflight()
    result = build_potential_scopes(
        reader,
        scanner,
        preflight,
        _snapshots(),
        (),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    assert result.scopes == ()
    assert result.preflight_request is None
    assert reader.calls == scanner.calls == preflight.calls == []


def test_recursive_dependency_uses_least_currently_queued_and_counts_unicode() -> None:
    z = _inventory("z.md", "Ж".encode(), None)
    a_path = RepoPath("ydb/docs/ru/a.md")
    a_target = RepoPath("ydb/docs/en/a.md")
    reader = _Reader({a_path: "é".encode(), a_target: None})
    scanner = _Scanner(
        {
            z.ru.path: (
                dependencies.DependencyLink(z.ru.path, a_path),
                dependencies.DependencyLink(z.ru.path, a_path),
            ),
            a_path: (),
        }
    )
    preflight = _Preflight()
    catalog = dependencies.RedirectCatalog(_snapshot(), _roots(), ())
    result = build_potential_scopes(reader, scanner, preflight, _snapshots(), (z,), catalog)
    assert tuple(call[1].value for call in scanner.calls) == (
        "ydb/docs/ru/z.md",
        "ydb/docs/ru/a.md",
    )
    assert tuple(call[1].value for call in reader.calls) == (
        "ydb/docs/ru/a.md",
        "ydb/docs/en/a.md",
    )
    assert len(preflight.calls) == 1
    scope = result.scopes[0]
    assert scope.direction is Direction.RU_TO_EN
    assert scope.measurement.dependency_file_count == 1
    assert scope.measurement.source_character_count == 2
    assert len(scope.dependencies) == 1
    assert [
        (entry.pair.source_path.value, entry.origin, entry.operation) for entry in scope.entries
    ] == [
        ("ydb/docs/ru/a.md", ScopeOrigin.DEPENDENCY, FileOperation.TRANSLATE),
        ("ydb/docs/ru/z.md", ScopeOrigin.INITIAL, FileOperation.TRANSLATE),
    ]


def test_invalid_utf8_stops_before_preflight() -> None:
    item = _inventory("bad.md", b"\xff", None)
    preflight = _Preflight()
    with pytest.raises(Exception) as caught:
        build_potential_scopes(
            _Reader({}),
            _Scanner({item.ru.path: ()}),
            preflight,
            _snapshots(),
            (item,),
            dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
        )
    assert caught.value.reason is ScopeInputReason.INVALID_UTF8_SOURCE
    assert preflight.calls == []


def test_b14_invalid_utf8_precedes_b15_dependency_target_collision() -> None:
    item = _inventory("a.md", b"\xff", None)
    dependency_source = RepoPath("ydb/docs/ru/b.md")
    reader = _Reader({dependency_source: b"dependency"})
    scanner = _Scanner(
        {
            item.ru.path: (
                dependencies.DependencyLink(item.ru.path, dependency_source),
            ),
            dependency_source: (),
        }
    )
    preflight = _Preflight()
    catalog = dependencies.RedirectCatalog(
        _snapshot(),
        _roots(),
        (
            dependencies.RedirectEntry(
                RepoPath("ydb/docs/en/b.md"),
                item.en.path,
            ),
        ),
    )

    with pytest.raises(InvalidScopeInput) as caught:
        build_potential_scopes(
            reader,
            scanner,
            preflight,
            _snapshots(),
            (item,),
            catalog,
        )

    assert caught.value.reason is ScopeInputReason.INVALID_UTF8_SOURCE
    assert caught.value.direction is Direction.RU_TO_EN
    assert caught.value.path == item.ru.path
    assert caught.value.args == ("invalid_scope_input:invalid_utf8_source",)
    assert tuple((path, content) for _, path, content in scanner.calls) == (
        (item.ru.path, b"\xff"),
        (dependency_source, b"dependency"),
    )
    assert tuple(path for _, path in reader.calls) == (dependency_source,)
    assert preflight.calls == []


def test_empty_existing_target_suppresses_dependency_entry() -> None:
    z = _inventory("z.md", b"z", None)
    a_path = RepoPath("ydb/docs/ru/a.md")
    a_target = RepoPath("ydb/docs/en/a.md")
    scanner = _Scanner({z.ru.path: (dependencies.DependencyLink(z.ru.path, a_path),)})
    result = build_potential_scopes(
        _Reader({a_path: b"a", a_target: b""}),
        scanner,
        _Preflight(),
        _snapshots(),
        (z,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    assert result.scopes[0].measurement.dependency_file_count == 0
    assert (
        result.scopes[0].dependencies[0].state
        is dependencies.DependencyResolutionState.TARGET_EXISTS
    )
    assert tuple(call[1] for call in scanner.calls) == (z.ru.path,)


def test_existing_target_expands_scope_for_missing_symmetric_linked_article() -> None:
    existing = _inventory("changelog.md", b"source", b"localized target")
    source_dependency = RepoPath("ydb/docs/ru/dev/optimization/hints.md")
    scanner = _Scanner(
        {
            existing.ru.path: (
                dependencies.DependencyLink(existing.ru.path, source_dependency),
            )
        }
    )

    result = build_potential_scopes(
        _Reader({source_dependency: b"source dependency"}),
        scanner,
        _Preflight(),
        _snapshots(),
        (existing,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )

    directional = result.scopes[0]
    assert directional.measurement.dependency_file_count == 1
    assert directional.dependencies[0].state is (
        dependencies.DependencyResolutionState.TARGET_MISSING_SOURCE_EXISTS
    )
    assert tuple(entry.origin for entry in directional.entries) == (
        ScopeOrigin.INITIAL,
        ScopeOrigin.DEPENDENCY,
    )
    assert tuple(call[1] for call in scanner.calls) == (
        existing.ru.path,
        source_dependency,
    )


def test_source_tombstone_wins_before_missing_source_and_does_not_scan() -> None:
    item = _inventory("z.md", None, b"old target")
    scanner = _Scanner({})
    result = build_potential_scopes(
        _Reader({}),
        scanner,
        _Preflight(),
        _snapshots(),
        (item,),
        dependencies.RedirectCatalog(
            _snapshot(),
            _roots(),
            (
                dependencies.RedirectEntry(
                    item.ru.path,
                    RepoPath("ydb/docs/ru/live.md"),
                ),
            ),
        ),
    )
    assert result.scopes[0].entries[0].operation is FileOperation.SKIP_SOURCE_TOMBSTONE
    assert scanner.calls == []


def test_diamond_is_scanned_once_and_uses_global_minimum_witness() -> None:
    z = _inventory("z.md", b"z", None)
    paths = {name: RepoPath(f"ydb/docs/ru/{name}.md") for name in "abc"}
    targets = {name: RepoPath(f"ydb/docs/en/{name}.md") for name in "abc"}
    scanner = _Scanner(
        {
            z.ru.path: tuple(dependencies.DependencyLink(z.ru.path, paths[name]) for name in "ab"),
            paths["a"]: (dependencies.DependencyLink(paths["a"], paths["c"]),),
            paths["b"]: (dependencies.DependencyLink(paths["b"], paths["c"]),),
            paths["c"]: (),
        }
    )
    values = {
        **{path: name.encode() for name, path in paths.items()},
        **{path: None for path in targets.values()},
    }
    reader = _Reader(values)
    result = build_potential_scopes(
        reader,
        scanner,
        _Preflight(),
        _snapshots(),
        (z,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    assert tuple(call[1].value for call in scanner.calls) == (
        "ydb/docs/ru/z.md",
        "ydb/docs/ru/a.md",
        "ydb/docs/ru/b.md",
        "ydb/docs/ru/c.md",
    )
    assert result.scopes[0].measurement.dependency_file_count == 3
    assert len(reader.calls) == len({call[1] for call in reader.calls})
    witnesses = result.scopes[0].measurement.dependency_witnesses
    assert (
        next(w for w in witnesses if w.dependency_source_path == paths["c"]).referring_source_path
        == paths["a"]
    )


def test_initial_origin_wins_when_an_initial_path_is_reached_as_dependency() -> None:
    a = _inventory("a.md", b"a", None)
    z = _inventory("z.md", b"z", None)
    scanner = _Scanner(
        {
            a.ru.path: (),
            z.ru.path: (dependencies.DependencyLink(z.ru.path, a.ru.path),),
        }
    )
    reader = _Reader({})
    result = build_potential_scopes(
        reader,
        scanner,
        _Preflight(),
        _snapshots(),
        (z, a),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    scope = result.scopes[0]
    assert scope.measurement.dependency_file_count == 0
    assert all(entry.origin is ScopeOrigin.INITIAL for entry in scope.entries)
    assert reader.calls == []
    assert tuple(call[1].value for call in scanner.calls) == (
        "ydb/docs/ru/a.md",
        "ydb/docs/ru/z.md",
    )


def test_mixed_job_builds_both_directions_and_calls_preflight_once_last() -> None:
    ru = _inventory("a.md", b"ru", b"en")
    en_key = PairKey(RepoPath("b.md"))
    roots = _roots()
    snapshot = _snapshot()
    en_path = RepoPath("ydb/docs/en/b.md")
    en_item = LocalePairInventory(
        roots,
        en_key,
        SnapshotLocaleFile(roots, Locale.RU, en_key, RepoPath("ydb/docs/ru/b.md"), snapshot, b"ru"),
        SnapshotLocaleFile(roots, Locale.EN, en_key, en_path, snapshot, b"en"),
        (
            ChangedMarkdownFile(
                roots,
                ChangedFileKind.MODIFIED,
                Locale.EN,
                en_key,
                en_path,
                en_path,
                None,
                None,
            ),
        ),
        PairFileState.BOTH_PRESENT,
    )
    events = []

    class Scanner(_Scanner):
        def links(self, snapshot, source_path, source_content, /):
            events.append(("scan", source_path.value))
            return super().links(snapshot, source_path, source_content)

    class Preflight(_Preflight):
        def check(self, request, /):
            events.append(("preflight", tuple(m.direction for m in request.measurements)))
            self.calls.append(request)

    preflight = Preflight()
    result = build_potential_scopes(
        _Reader({}),
        Scanner({}),
        preflight,
        _snapshots(),
        (en_item, ru),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    assert tuple(item.direction for item in result.scopes) == (
        Direction.RU_TO_EN,
        Direction.EN_TO_RU,
    )
    assert len(preflight.calls) == 1
    assert tuple(item.direction for item in preflight.calls[0].measurements) == (
        Direction.RU_TO_EN,
        Direction.EN_TO_RU,
    )
    assert events[-1][0] == "preflight"


def test_preflight_exception_propagates_without_retry() -> None:
    item = _inventory("z.md", b"z", None)
    sentinel = RuntimeError("sentinel")

    class Reject:
        calls = 0

        def check(self, request, /):
            self.calls += 1
            raise sentinel

    preflight = Reject()
    with pytest.raises(RuntimeError) as caught:
        build_potential_scopes(
            _Reader({}),
            _Scanner({item.ru.path: ()}),
            preflight,
            _merged_snapshots(),
            (item,),
            dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
        )
    assert caught.value is sentinel
    assert preflight.calls == 1


def test_non_none_preflight_result_is_rejected_without_retry() -> None:
    item = _inventory("z.md", b"z", None)

    class InvalidReturn:
        calls = 0

        def check(self, request, /):
            self.calls += 1
            return object()

    preflight = InvalidReturn()
    with pytest.raises(Exception) as caught:
        build_potential_scopes(
            _Reader({}),
            _Scanner({item.ru.path: ()}),
            preflight,
            _snapshots(),
            (item,),
            dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
        )
    assert caught.value.reason is ScopeInputReason.INVALID_PREFLIGHT_RESULT
    assert preflight.calls == 1


def test_freeze_rejects_equal_but_not_identical_inventory() -> None:
    item = _inventory("z.md", b"z", None)
    potential = build_potential_scopes(
        _Reader({}),
        _Scanner({item.ru.path: ()}),
        _Preflight(),
        _snapshots(),
        (item,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    equal_copy = _inventory("z.md", b"z", None)
    decision = DirectionSelectionResult(
        DirectionSelectionState.SELECTED,
        Direction.RU_TO_EN,
        (DirectionPairDecision(equal_copy, DirectionPairVerdict.RU_TO_EN),),
        None,
    )
    with pytest.raises(Exception) as caught:
        freeze_scope_manifest(potential, decision)
    assert caught.value.reason is ScopeInputReason.DIRECTION_RESULT_MISMATCH


def test_strict_subclass_and_non_echo_contracts() -> None:
    class PathSubclass(RepoPath):
        pass

    canary = "TOP-SECRET-PATH.md"
    with pytest.raises(Exception) as caught:
        dependencies.DependencyLink(PathSubclass("x.md"), RepoPath("y.md"))
    assert "DependencyLink.source_path" in str(caught.value)
    error = dependencies.InvalidDependencyInput(
        dependencies.DependencyInputReason.NON_MARKDOWN_DESTINATION,
        RepoPath(canary),
    )
    assert canary not in str(error)


def _rename_inventory(state: RenameContentState) -> LocalePairInventory:
    roots = _roots()
    snapshot = _snapshot()
    key = PairKey(RepoPath("new.md"))
    previous = PairKey(RepoPath("old.md"))
    ru_path = RepoPath("ydb/docs/ru/new.md")
    return LocalePairInventory(
        roots,
        key,
        SnapshotLocaleFile(roots, Locale.RU, key, ru_path, snapshot, b"same bytes"),
        SnapshotLocaleFile(
            roots,
            Locale.EN,
            key,
            RepoPath("ydb/docs/en/new.md"),
            snapshot,
            None,
        ),
        (
            ChangedMarkdownFile(
                roots,
                ChangedFileKind.RENAMED,
                Locale.RU,
                key,
                RepoPath("ydb/docs/ru/old.md"),
                ru_path,
                previous,
                state,
            ),
        ),
        PairFileState.RU_ONLY,
    )


def test_rename_content_state_is_authoritative_with_equal_bytes_and_reads_old_pair_once() -> None:
    operations = []
    for rename_state in (RenameContentState.UNCHANGED, RenameContentState.CHANGED):
        item = _rename_inventory(rename_state)
        reader = _Reader(
            {
                RepoPath("ydb/docs/ru/old.md"): b"same bytes",
                RepoPath("ydb/docs/en/old.md"): b"same bytes",
            }
        )
        result = build_potential_scopes(
            reader,
            _Scanner({item.ru.path: ()}),
            _Preflight(),
            _merged_snapshots(),
            (item,),
            dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
        )
        operations.append(result.scopes[0].entries[0].operation)
        assert tuple((snapshot, path.value) for snapshot, path in reader.calls) == (
            (_snapshot(), "ydb/docs/ru/old.md"),
            (_snapshot(), "ydb/docs/en/old.md"),
        )
    assert operations == [FileOperation.RENAME_TARGET, FileOperation.RENAME_TARGET_AND_TRANSLATE]


@pytest.mark.parametrize(
    ("ru", "en", "operation"),
    [
        (None, b"target", FileOperation.DELETE_TARGET),
        (None, None, FileOperation.NOOP_TARGET_ABSENT),
        (b"source", None, FileOperation.TRANSLATE),
        (b"source", b"target", FileOperation.TRANSLATE),
    ],
)
def test_current_tip_add_modify_delete_and_noop_states(ru, en, operation) -> None:
    item = _inventory("state.md", ru, en)
    result = build_potential_scopes(
        _Reader({}),
        _Scanner({item.ru.path: ()}),
        _Preflight(),
        _snapshots(),
        (item,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    assert result.scopes[0].entries[0].operation is operation


def test_all_thirteen_records_are_frozen_slotted_non_serializable_and_strict() -> None:
    item = _inventory("z.md", b"z", None)
    a_path = RepoPath("ydb/docs/ru/a.md")
    a_target = RepoPath("ydb/docs/en/a.md")
    potential = build_potential_scopes(
        _Reader({a_path: b"a", a_target: None}),
        _Scanner(
            {
                item.ru.path: (dependencies.DependencyLink(item.ru.path, a_path),),
                a_path: (),
            }
        ),
        _Preflight(),
        _snapshots(),
        (item,),
        dependencies.RedirectCatalog(
            _snapshot(),
            _roots(),
            (
                dependencies.RedirectEntry(
                    RepoPath("ydb/docs/ru/legacy.md"),
                    RepoPath("ydb/docs/ru/live.md"),
                ),
            ),
        ),
    )
    direction_result = DirectionSelectionResult(
        DirectionSelectionState.SELECTED,
        Direction.RU_TO_EN,
        (DirectionPairDecision(item, DirectionPairVerdict.RU_TO_EN),),
        None,
    )
    selection = freeze_scope_manifest(potential, direction_result)
    assert selection.state is ScopeSelectionState.SELECTED
    scope = potential.scopes[0]
    assert selection.manifest is not None
    records = (
        scope.dependencies[0].link,
        potential.preflight_request,
        dependencies.RedirectEntry(RepoPath("ydb/docs/ru/x.md"), RepoPath("ydb/docs/ru/y.md")),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
        scope.dependencies[0],
        scope.entries[0],
        scope.measurement.dependency_witnesses[0],
        scope.measurement,
        scope,
        potential,
        selection.manifest.initial_outcomes[0],
        selection.manifest,
        selection,
    )
    assert len(records) == 13
    for record in records:
        assert record is not None
        assert "__dict__" not in type(record).__slots__
        with pytest.raises(FrozenInstanceError):
            setattr(record, fields(record)[0].name, fields(record)[0].name)
        with pytest.raises(UnknownDomainType):
            to_wire(record)
    with pytest.raises(InvariantViolation):
        replace(scope.entries[0], initial_keys=[item.key])


def test_content_bytes_are_hidden_from_repr() -> None:
    canary = b"NEVER-PRINT-THIS-CONTENT"
    item = _inventory("secret.md", canary, canary)
    potential = build_potential_scopes(
        _Reader({}),
        _Scanner({item.ru.path: ()}),
        _Preflight(),
        _snapshots(),
        (item,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    assert "NEVER-PRINT-THIS-CONTENT" not in repr(potential.scopes[0].entries[0])


def test_concrete_error_contracts_have_exact_safe_args_and_attributes() -> None:
    path = RepoPath("secret/path.md")
    dep = dependencies.InvalidDependencyInput(
        dependencies.DependencyInputReason.SOURCE_PATH_MISMATCH, path
    )
    assert dep.args == ("invalid_dependency_input:source_path_mismatch",)
    assert dep.path is path
    cycle = dependencies.RedirectCycleDetected(Locale.RU, (path, path))
    assert cycle.args == ("redirect_cycle_detected",)
    assert cycle.paths == (path, path)
    scoped = __import__("ydbdoc_review_ng.scope", fromlist=["InvalidScopeInput"])
    error = scoped.InvalidScopeInput(
        ScopeInputReason.TARGET_PATH_COLLISION, Direction.RU_TO_EN, path
    )
    assert error.args == ("invalid_scope_input:target_path_collision",)
    assert error.direction is Direction.RU_TO_EN
    assert "secret/path.md" not in str(error)
    assert str(inspect.signature(dependencies.resolve_redirect)) == (
        "(catalog: 'RedirectCatalog', locale: 'Locale', path: 'RepoPath', /) -> 'RepoPath'"
    )


def test_dependency_enum_names_and_wire_values_are_exact() -> None:
    assert {item.name: item.value for item in dependencies.DependencyInputReason} == {
        "INVALID_SCANNER_RESULT": "invalid_scanner_result",
        "SOURCE_PATH_MISMATCH": "source_path_mismatch",
        "NON_MARKDOWN_DESTINATION": "non_markdown_destination",
        "MIXED_LOCALE": "mixed_locale",
        "PATH_OUTSIDE_SOURCE_LOCALE": "path_outside_source_locale",
        "NON_MARKDOWN_REDIRECT": "non_markdown_redirect",
        "DUPLICATE_REDIRECT_FROM": "duplicate_redirect_from",
        "REDIRECT_CROSSES_LOCALES": "redirect_crosses_locales",
        "REDIRECT_SELF_LOOP": "redirect_self_loop",
    }
    assert {item.name: item.value for item in dependencies.DependencyResolutionState} == {
        "TARGET_EXISTS": "target_exists",
        "TARGET_REDIRECT_EXISTS": "target_redirect_exists",
        "TARGET_MISSING_SOURCE_EXISTS": "target_missing_source_exists",
        "SOURCE_MISSING": "source_missing",
    }


def test_a008_a010_a011_real_pr51079_payloads_flow_through_build() -> None:
    # Synthetic topology over attributed PR 51079 document bytes.
    fixture_root = Path(__file__).parents[1] / "fixtures" / "scope" / "pr51079"
    auth_config = (fixture_root / "auth_config.slice").read_bytes()
    authentication = (fixture_root / "authentication.slice").read_bytes()
    fixture_objects = (auth_config, authentication)
    assert len(auth_config) == 397
    assert len(auth_config.decode("utf-8")) == 244
    assert hashlib.sha256(auth_config).hexdigest() == (
        "b4e37962224c79ce22b02deb9571df0fcd99974eba50ded4a897e34022dcf770"
    )
    assert len(authentication) == 694
    assert len(authentication.decode("utf-8")) == 431
    assert hashlib.sha256(authentication).hexdigest() == (
        "d13d910ccaefec0eab57c927317caa5f863081455a6a2371192a57443548a80b"
    )

    initial_source = RepoPath("ydb/docs/ru/core/reference/configuration/auth_config.md")
    dependency_source = RepoPath("ydb/docs/ru/core/security/authentication.md")
    dependency_target = RepoPath("ydb/docs/en/core/security/authentication.md")
    key = PairKey(RepoPath("core/reference/configuration/auth_config.md"))
    roots = _roots()
    change = ChangedMarkdownFile(
        roots,
        ChangedFileKind.ADDED,
        Locale.RU,
        key,
        None,
        initial_source,
        None,
        None,
    )
    inventory = LocalePairInventory(
        roots,
        key,
        SnapshotLocaleFile(roots, Locale.RU, key, initial_source, _snapshot(), auth_config),
        SnapshotLocaleFile(
            roots,
            Locale.EN,
            key,
            RepoPath("ydb/docs/en/core/reference/configuration/auth_config.md"),
            _snapshot(),
            None,
        ),
        (change,),
        PairFileState.RU_ONLY,
    )
    reader = _Reader({dependency_source: authentication, dependency_target: None})
    scanner = _Scanner(
        {
            initial_source: (dependencies.DependencyLink(initial_source, dependency_source),),
            dependency_source: (),
        }
    )
    preflight = _Preflight()
    result = build_potential_scopes(
        reader,
        scanner,
        preflight,
        _snapshots(),
        (inventory,),
        dependencies.RedirectCatalog(_snapshot(), roots, ()),
    )

    assert tuple(call[1] for call in scanner.calls) == (initial_source, dependency_source)
    assert tuple(reader.calls) == (
        (_snapshot(), dependency_source),
        (_snapshot(), dependency_target),
    )
    assert tuple(item.direction for item in result.scopes) == (Direction.RU_TO_EN,)
    directional = result.scopes[0]
    assert len(directional.dependencies) == 1
    assert directional.dependencies[0] == dependencies.ResolvedDependency(
        dependencies.DependencyLink(initial_source, dependency_source),
        dependency_source,
        dependency_target,
        dependencies.DependencyResolutionState.TARGET_MISSING_SOURCE_EXISTS,
    )
    assert tuple((entry.origin, entry.operation) for entry in directional.entries) == (
        (ScopeOrigin.INITIAL, FileOperation.TRANSLATE),
        (ScopeOrigin.DEPENDENCY, FileOperation.TRANSLATE),
    )
    entries_by_source = {entry.pair.source_path: entry for entry in directional.entries}
    assert entries_by_source[initial_source].source_content is fixture_objects[0]
    assert entries_by_source[dependency_source].source_content is fixture_objects[1]
    assert directional.measurement.dependency_file_count == 1
    assert directional.measurement.source_character_count == 675
    assert directional.measurement.dependency_witnesses == (
        __import__("ydbdoc_review_ng.scope", fromlist=["DependencyWitness"]).DependencyWitness(
            dependency_source, initial_source
        ),
    )
    assert len(preflight.calls) == 1
    assert preflight.calls[0] is result.preflight_request
    assert preflight.calls[0].measurements == (directional.measurement,)
    assert preflight.calls[0].measurements[0].source_character_count == 675


def test_a004_a009_retained_redirect_and_source_missing_matrix() -> None:
    initial = _inventory("z.md", b"z", None)
    z_path = initial.ru.path
    ru_a = RepoPath("ydb/docs/ru/a.md")
    ru_b = RepoPath("ydb/docs/ru/b.md")
    ru_c = RepoPath("ydb/docs/ru/c.md")
    en_a = RepoPath("ydb/docs/en/a.md")
    en_b = RepoPath("ydb/docs/en/b.md")
    en_c = RepoPath("ydb/docs/en/c.md")
    link = dependencies.DependencyLink(z_path, ru_a)

    source_chain = dependencies.RedirectCatalog(
        _snapshot(),
        _roots(),
        (
            dependencies.RedirectEntry(ru_b, ru_c),
            dependencies.RedirectEntry(ru_a, ru_b),
        ),
    )
    source_result = build_potential_scopes(
        _Reader({ru_c: b"terminal source", en_c: None}),
        _Scanner({z_path: (link,), ru_c: ()}),
        _Preflight(),
        _snapshots(),
        (initial,),
        source_chain,
    )
    source_dependency = source_result.scopes[0].dependencies[0]
    assert source_dependency.source_path == ru_c
    assert source_dependency.target_path == en_c
    assert (
        source_dependency.state
        is dependencies.DependencyResolutionState.TARGET_MISSING_SOURCE_EXISTS
    )
    assert tuple(
        entry.pair.source_path
        for entry in source_result.scopes[0].entries
        if entry.origin is ScopeOrigin.DEPENDENCY
    ) == (ru_c,)

    target_chain = dependencies.RedirectCatalog(
        _snapshot(),
        _roots(),
        (
            dependencies.RedirectEntry(en_b, en_c),
            dependencies.RedirectEntry(en_a, en_b),
        ),
    )
    target_result = build_potential_scopes(
        _Reader({ru_a: b"source", en_c: b""}),
        _Scanner({z_path: (link,)}),
        _Preflight(),
        _snapshots(),
        (initial,),
        target_chain,
    )
    target_dependency = target_result.scopes[0].dependencies[0]
    assert target_dependency.source_path == ru_a
    assert target_dependency.target_path == en_c
    assert (
        target_dependency.state is dependencies.DependencyResolutionState.TARGET_REDIRECT_EXISTS
    )
    assert all(entry.origin is ScopeOrigin.INITIAL for entry in target_result.scopes[0].entries)

    missing_result = build_potential_scopes(
        _Reader({ru_c: None}),
        _Scanner({z_path: (link,)}),
        _Preflight(),
        _snapshots(),
        (initial,),
        source_chain,
    )
    missing_dependency = missing_result.scopes[0].dependencies[0]
    assert missing_dependency.source_path == ru_c
    assert missing_dependency.target_path == en_c
    assert missing_dependency.state is dependencies.DependencyResolutionState.SOURCE_MISSING
    assert all(entry.origin is ScopeOrigin.INITIAL for entry in missing_result.scopes[0].entries)

    cycle_catalog = dependencies.RedirectCatalog(
        _snapshot(),
        _roots(),
        (
            dependencies.RedirectEntry(ru_c, ru_a),
            dependencies.RedirectEntry(ru_a, ru_b),
            dependencies.RedirectEntry(ru_b, ru_c),
        ),
    )
    with pytest.raises(dependencies.RedirectCycleDetected) as caught:
        build_potential_scopes(
            _Reader({}),
            _Scanner({z_path: (link,)}),
            _Preflight(),
            _snapshots(),
            (initial,),
            cycle_catalog,
        )
    assert caught.value.locale is Locale.RU
    assert caught.value.paths == (ru_a, ru_b, ru_c, ru_a)
    assert caught.value.args == ("redirect_cycle_detected",)


def test_a015_permutation_and_hash_seed_invariance() -> None:
    program = textwrap.dedent(
        """
        import json
        import os

        from ydbdoc_review_ng import dependencies
        from ydbdoc_review_ng.direction import Direction
        from ydbdoc_review_ng.domain import GitSha, Locale, RepoPath, RepositoryId, SnapshotRef
        from ydbdoc_review_ng.locales import (
            ChangedFileKind, ChangedMarkdownFile, LocalePairInventory, LocaleRoots,
            PairFileState, PairKey, SnapshotLocaleFile,
        )
        from ydbdoc_review_ng.repository import (
            BaseBranch, PullRequestState, ResolvedRepositorySnapshots,
        )
        from ydbdoc_review_ng.scope import build_potential_scopes

        snapshot = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
        roots = LocaleRoots(RepoPath("ydb/docs/ru"), RepoPath("ydb/docs/en"))

        def inventory(name, content):
            key = PairKey(RepoPath(name))
            ru_path = RepoPath(f"{roots.ru.value}/{name}")
            en_path = RepoPath(f"{roots.en.value}/{name}")
            change = ChangedMarkdownFile(
                roots, ChangedFileKind.ADDED, Locale.RU, key, None, ru_path, None, None
            )
            return LocalePairInventory(
                roots,
                key,
                SnapshotLocaleFile(roots, Locale.RU, key, ru_path, snapshot, content),
                SnapshotLocaleFile(roots, Locale.EN, key, en_path, snapshot, None),
                (change,),
                PairFileState.RU_ONLY,
            )

        z = inventory("z.md", b"z")
        x = inventory("x.md", b"x")
        a = RepoPath("ydb/docs/ru/a.md")
        b = RepoPath("ydb/docs/ru/b.md")
        en_a = RepoPath("ydb/docs/en/a.md")
        en_b = RepoPath("ydb/docs/en/b.md")
        seed = int(os.environ["PYTHONHASHSEED"])
        inventories = [z, x]
        if seed % 2:
            inventories.reverse()
        redirect_entries = [
            dependencies.RedirectEntry(x.ru.path, RepoPath("ydb/docs/ru/x-live.md")),
            dependencies.RedirectEntry(
                RepoPath("ydb/docs/ru/q.md"), RepoPath("ydb/docs/ru/r.md")
            ),
        ]
        if seed % 3:
            redirect_entries.reverse()
        scanner_links = [
            dependencies.DependencyLink(z.ru.path, a),
            dependencies.DependencyLink(z.ru.path, b),
        ]
        if seed % 2:
            scanner_links.reverse()

        class Reader:
            def __init__(self):
                self.values = {a: b"a", b: b"b", en_a: None, en_b: None}
                self.calls = []
            def read_bytes(self, current_snapshot, path, /):
                self.calls.append(path)
                return self.values.get(path)

        class Scanner:
            def __init__(self):
                self.calls = []
                self.by_path = {
                    z.ru.path: tuple(scanner_links),
                    a: (),
                    b: (),
                }
            def links(self, current_snapshot, source_path, source_content, /):
                self.calls.append(source_path)
                return self.by_path.get(source_path, ())

        class Preflight:
            def check(self, request, /):
                return None

        snapshots = ResolvedRepositorySnapshots(
            PullRequestState.OPEN, BaseBranch("main"), snapshot, snapshot, snapshot,
            snapshot, snapshot, None, None,
        )
        scanner = Scanner()
        reader = Reader()
        result = build_potential_scopes(
            reader,
            scanner,
            Preflight(),
            snapshots,
            tuple(inventories),
            dependencies.RedirectCatalog(snapshot, roots, tuple(redirect_entries)),
        )
        projection = {
            "directions": [item.direction.value for item in result.scopes],
            "entries": [
                [
                    entry.pair.target_path.value,
                    entry.pair.source_path.value,
                    entry.operation.value,
                    entry.origin.value,
                ]
                for item in result.scopes
                for entry in item.entries
            ],
            "dependencies": [
                [
                    dependency.link.source_path.value,
                    dependency.link.destination_source_path.value,
                    dependency.source_path.value,
                    dependency.target_path.value,
                    dependency.state.value,
                ]
                for item in result.scopes
                for dependency in item.dependencies
            ],
            "witnesses": [
                [witness.dependency_source_path.value, witness.referring_source_path.value]
                for item in result.scopes
                for witness in item.measurement.dependency_witnesses
            ],
            "counts": [
                [item.measurement.dependency_file_count, item.measurement.source_character_count]
                for item in result.scopes
            ],
            "scan": [path.value.rsplit("/", 1)[-1] for path in scanner.calls],
            "reads": [path.value for path in reader.calls],
        }
        print(json.dumps(projection, sort_keys=True, separators=(",", ":")))
        """
    )
    encoded: list[bytes] = []
    objects: list[dict[str, object]] = []
    for seed in (1, 2, 3, 17, 101):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = str(seed)
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=Path(__file__).parents[2],
            env=environment,
            check=True,
            capture_output=True,
        )
        payload = completed.stdout.strip()
        encoded.append(payload)
        objects.append(json.loads(payload))
    assert all(payload == encoded[0] for payload in encoded)
    assert all(item == objects[0] for item in objects)
    assert all(item["scan"] == ["z.md", "a.md", "b.md"] for item in objects)
    assert objects[0]["scan"][0:2] == ["z.md", "a.md"]
