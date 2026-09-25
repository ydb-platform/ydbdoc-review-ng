from __future__ import annotations

import inspect
from dataclasses import fields, is_dataclass
from enum import Enum

import pytest

from ydbdoc_review_ng import dependencies, scope
from ydbdoc_review_ng.direction import (
    DIRECTION_UNDETERMINED_ACTION,
    DIRECTION_UNDETERMINED_WARNING,
    Direction,
    DirectionPairDecision,
    DirectionPairVerdict,
    DirectionSelectionResult,
    DirectionSelectionState,
)
from ydbdoc_review_ng.domain import (
    Diagnostic,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    Severity,
    SnapshotRef,
)
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


def test_public_scope_contract_inventory() -> None:
    expected = {
        "ScopeOrigin",
        "FileOperation",
        "InitialPairDisposition",
        "ScopeSelectionState",
        "ScopeInputReason",
        "ScopeEntry",
        "DependencyWitness",
        "ScopeMeasurement",
        "ScopePreflightRequest",
        "DirectionalPotentialScope",
        "PotentialScopeSet",
        "InitialPairOutcome",
        "ScopeManifest",
        "ScopeSelection",
        "ScopePreflight",
        "ScopeError",
        "InvalidScopeInput",
        "build_potential_scopes",
        "freeze_scope_manifest",
    }
    assert set(scope.__all__) == expected
    assert inspect.signature(scope.ScopePreflight.check) == inspect.Signature(
        [
            inspect.Parameter("self", inspect.Parameter.POSITIONAL_ONLY),
            inspect.Parameter(
                "request",
                inspect.Parameter.POSITIONAL_ONLY,
                annotation="ScopePreflightRequest",
            ),
        ],
        return_annotation="None",
    )
    assert len(tuple(scope.ScopeOrigin)) == 2
    assert len(tuple(scope.FileOperation)) == 8
    assert len(tuple(scope.InitialPairDisposition)) == 2
    assert len(tuple(scope.ScopeSelectionState)) == 3
    assert len(tuple(scope.ScopeInputReason)) == 11
    assert all(
        issubclass(item, str) and issubclass(item, Enum)
        for item in (
            scope.ScopeOrigin,
            scope.FileOperation,
            scope.InitialPairDisposition,
            scope.ScopeSelectionState,
            scope.ScopeInputReason,
        )
    )


@pytest.mark.parametrize(
    ("record", "field_names"),
    [
        (
            scope.ScopeEntry,
            (
                "pair",
                "source_content",
                "target_content",
                "origin",
                "operation",
                "initial_keys",
                "rename_from_target_path",
                "rename_from_target_content",
            ),
        ),
        (scope.DependencyWitness, ("dependency_source_path", "referring_source_path")),
        (
            scope.ScopeMeasurement,
            (
                "direction",
                "dependency_file_count",
                "source_character_count",
                "dependency_witnesses",
            ),
        ),
        (scope.ScopePreflightRequest, ("scope_snapshot", "measurements")),
        (
            scope.DirectionalPotentialScope,
            (
                "direction",
                "scope_snapshot",
                "roots",
                "initial_pairs",
                "entries",
                "dependencies",
                "measurement",
            ),
        ),
        (scope.PotentialScopeSet, ("scope_snapshot", "roots", "scopes", "preflight_request")),
        (scope.InitialPairOutcome, ("key", "disposition", "entry")),
        (
            scope.ScopeManifest,
            (
                "direction",
                "scope_snapshot",
                "roots",
                "initial_outcomes",
                "entries",
                "dependency_file_count",
                "source_character_count",
            ),
        ),
        (scope.ScopeSelection, ("state", "manifest")),
    ],
)
def test_scope_records_are_frozen_slotted_with_exact_field_order(
    record: type[object], field_names: tuple[str, ...]
) -> None:
    assert is_dataclass(record)
    assert tuple(item.name for item in fields(record)) == field_names
    assert "__dict__" not in record.__slots__


def test_scope_function_signatures_are_positional_only() -> None:
    assert str(inspect.signature(scope.build_potential_scopes)) == (
        "(reader: 'SnapshotReader', dependency_source: 'DependencySource', "
        "preflight: 'ScopePreflight', snapshots: 'ResolvedRepositorySnapshots', "
        "inventories: 'tuple[LocalePairInventory, ...]', redirects: 'RedirectCatalog', /) "
        "-> 'PotentialScopeSet'"
    )
    assert str(inspect.signature(scope.freeze_scope_manifest)) == (
        "(potential: 'PotentialScopeSet', direction_result: 'DirectionSelectionResult', /) "
        "-> 'ScopeSelection'"
    )


def test_scope_enum_names_and_wire_values_are_exact() -> None:
    assert {item.name: item.value for item in scope.ScopeOrigin} == {
        "INITIAL": "initial",
        "DEPENDENCY": "dependency",
    }
    assert {item.name: item.value for item in scope.FileOperation} == {
        "TRANSLATE": "translate",
        "DELETE_TARGET": "delete_target",
        "RENAME_TARGET": "rename_target",
        "RENAME_TARGET_AND_TRANSLATE": "rename_target_and_translate",
        "NOOP_TARGET_ABSENT": "noop_target_absent",
        "NOOP_TARGET_ALREADY_RENAMED": "noop_target_already_renamed",
        "SKIP_SOURCE_TOMBSTONE": "skip_source_tombstone",
        "SKIP_TARGET_TOMBSTONE": "skip_target_tombstone",
    }
    assert {item.name: item.value for item in scope.InitialPairDisposition} == {
        "SELECTED": "selected",
        "COMPLETE_PAIR": "complete_pair",
    }
    assert {item.name: item.value for item in scope.ScopeSelectionState} == {
        "SELECTED": "selected",
        "NO_TRANSLATE": "no_translate",
        "DIRECTION_UNDETERMINED": "direction_undetermined",
    }
    assert {item.name: item.value for item in scope.ScopeInputReason} == {
        "EMPTY_INITIAL_CHANGES": "empty_initial_changes",
        "DUPLICATE_PAIR_KEY": "duplicate_pair_key",
        "MIXED_ROOTS": "mixed_roots",
        "MIXED_SCOPE_SNAPSHOT": "mixed_scope_snapshot",
        "SNAPSHOT_MISMATCH": "snapshot_mismatch",
        "DIRECTION_NOT_POTENTIAL": "direction_not_potential",
        "DIRECTION_RESULT_MISMATCH": "direction_result_mismatch",
        "TARGET_PATH_COLLISION": "target_path_collision",
        "AMBIGUOUS_RENAME_TARGET": "ambiguous_rename_target",
        "INVALID_UTF8_SOURCE": "invalid_utf8_source",
        "INVALID_PREFLIGHT_RESULT": "invalid_preflight_result",
    }


def _snapshot() -> SnapshotRef:
    return SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


def _roots() -> LocaleRoots:
    return LocaleRoots(RepoPath("ydb/docs/ru"), RepoPath("ydb/docs/en"))


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


class _Reader:
    def __init__(self, values: dict[RepoPath, bytes | None]) -> None:
        self.values = values
        self.calls: list[tuple[SnapshotRef, RepoPath]] = []

    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
        self.calls.append((snapshot, path))
        return self.values.get(path)


class _Scanner:
    def __init__(self, links: dict[RepoPath, tuple[dependencies.DependencyLink, ...]]) -> None:
        self.links_by_path = links
        self.calls: list[tuple[SnapshotRef, RepoPath, bytes]] = []

    def links(
        self, snapshot: SnapshotRef, source_path: RepoPath, source_content: bytes, /
    ) -> tuple[dependencies.DependencyLink, ...]:
        self.calls.append((snapshot, source_path, source_content))
        return self.links_by_path.get(source_path, ())


class _Preflight:
    def __init__(self) -> None:
        self.calls: list[scope.ScopePreflightRequest] = []

    def check(self, request: scope.ScopePreflightRequest, /) -> None:
        self.calls.append(request)


def _pair_state(ru: bytes | None, en: bytes | None) -> PairFileState:
    if ru is not None and en is not None:
        return PairFileState.BOTH_PRESENT
    if ru is not None:
        return PairFileState.RU_ONLY
    if en is not None:
        return PairFileState.EN_ONLY
    return PairFileState.BOTH_MISSING


def _change(
    locale: Locale,
    kind: ChangedFileKind,
    key: PairKey,
    *,
    previous: PairKey | None = None,
    rename_state: RenameContentState | None = None,
) -> ChangedMarkdownFile:
    roots = _roots()
    root = roots.ru if locale is Locale.RU else roots.en
    current = RepoPath(f"{root.value}/{key.relative_path.value}")
    old = (
        RepoPath(f"{root.value}/{previous.relative_path.value}")
        if previous is not None
        else current
    )
    return ChangedMarkdownFile(
        roots,
        kind,
        locale,
        key,
        None if kind is ChangedFileKind.ADDED else old,
        None if kind is ChangedFileKind.DELETED else current,
        previous,
        rename_state,
    )


def _inventory(
    key_value: str,
    ru: bytes | None,
    en: bytes | None,
    changes: tuple[ChangedMarkdownFile, ...],
) -> LocalePairInventory:
    roots = _roots()
    key = PairKey(RepoPath(key_value))
    return LocalePairInventory(
        roots,
        key,
        SnapshotLocaleFile(
            roots,
            Locale.RU,
            key,
            RepoPath(f"{roots.ru.value}/{key_value}"),
            _snapshot(),
            ru,
        ),
        SnapshotLocaleFile(
            roots,
            Locale.EN,
            key,
            RepoPath(f"{roots.en.value}/{key_value}"),
            _snapshot(),
            en,
        ),
        changes,
        _pair_state(ru, en),
    )


def _rename_matrix_inventory(
    direction: Direction,
    old_exists: bool,
    new_exists: bool,
    metadata: str,
    rename_state: RenameContentState,
) -> tuple[LocalePairInventory, dict[RepoPath, bytes | None]]:
    source_locale = Locale.RU if direction is Direction.RU_TO_EN else Locale.EN
    target_locale = Locale.EN if direction is Direction.RU_TO_EN else Locale.RU
    key = PairKey(RepoPath("new.md"))
    previous = PairKey(RepoPath("old.md"))
    other_previous = PairKey(RepoPath("other.md"))
    source_change = _change(
        source_locale,
        ChangedFileKind.RENAMED,
        key,
        previous=previous,
        rename_state=rename_state,
    )
    target_change: ChangedMarkdownFile | None = None
    if metadata == "PAIR":
        target_change = _change(
            target_locale,
            ChangedFileKind.RENAMED,
            key,
            previous=previous,
            rename_state=RenameContentState.UNCHANGED,
        )
    elif metadata == "ADD":
        target_change = _change(target_locale, ChangedFileKind.ADDED, key)
    elif metadata == "MOD":
        target_change = _change(target_locale, ChangedFileKind.MODIFIED, key)
    elif metadata == "OTHER":
        target_change = _change(
            target_locale,
            ChangedFileKind.RENAMED,
            key,
            previous=other_previous,
            rename_state=RenameContentState.UNCHANGED,
        )
    changes = tuple(
        sorted(
            (source_change,) if target_change is None else (source_change, target_change),
            key=lambda item: 0 if item.locale is Locale.RU else 1,
        )
    )
    source_content = b"source-current"
    target_content = b"target-current" if new_exists else None
    ru = source_content if source_locale is Locale.RU else target_content
    en = source_content if source_locale is Locale.EN else target_content
    inventory = _inventory("new.md", ru, en, changes)
    source_root = _roots().ru if source_locale is Locale.RU else _roots().en
    target_root = _roots().en if target_locale is Locale.EN else _roots().ru
    values = {
        RepoPath(f"{source_root.value}/old.md"): None,
        RepoPath(f"{target_root.value}/old.md"): b"old-target" if old_exists else None,
        RepoPath(f"{_roots().ru.value}/other.md"): None,
        RepoPath(f"{_roots().en.value}/other.md"): None,
    }
    return inventory, values


def test_a006_public_rename_matrix_and_combined_invalid_precedence() -> None:
    operation_counts = {Direction.RU_TO_EN: 0, Direction.EN_TO_RU: 0}
    collision_counts = {Direction.RU_TO_EN: 0, Direction.EN_TO_RU: 0}
    ambiguous_counts = {Direction.RU_TO_EN: 0, Direction.EN_TO_RU: 0}
    attributed_counts = {Direction.RU_TO_EN: 0, Direction.EN_TO_RU: 0}
    combined_invalid_count = 0

    for direction in (Direction.RU_TO_EN, Direction.EN_TO_RU):
        for old_exists, new_exists in ((False, False), (False, True), (True, False), (True, True)):
            for metadata in ("ABS", "PAIR", "ADD", "MOD", "OTHER"):
                for rename_state in (RenameContentState.UNCHANGED, RenameContentState.CHANGED):
                    inventory, values = _rename_matrix_inventory(
                        direction, old_exists, new_exists, metadata, rename_state
                    )
                    preflight = _Preflight()
                    source = inventory.ru if direction is Direction.RU_TO_EN else inventory.en
                    target = inventory.en if direction is Direction.RU_TO_EN else inventory.ru
                    old_target = RepoPath(
                        f"{_roots().en.value if direction is Direction.RU_TO_EN else _roots().ru.value}/old.md"
                    )
                    combined_invalid = (
                        direction is Direction.EN_TO_RU
                        and metadata == "OTHER"
                        and new_exists
                    )
                    if combined_invalid:
                        combined_invalid_count += 1
                        with pytest.raises(scope.InvalidScopeInput) as caught:
                            scope.build_potential_scopes(
                                _Reader(values),
                                _Scanner({source.path: ()}),
                                preflight,
                                _snapshots(),
                                (inventory,),
                                dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
                            )
                        assert caught.value.reason is scope.ScopeInputReason.TARGET_PATH_COLLISION
                        assert caught.value.direction is Direction.RU_TO_EN
                        assert caught.value.path == RepoPath("ydb/docs/en/new.md")
                        assert caught.value.args == (
                            "invalid_scope_input:target_path_collision",
                        )
                        assert preflight.calls == []
                        collision_counts[Direction.RU_TO_EN] += 1
                        attributed_counts[Direction.RU_TO_EN] += 1
                        continue

                    expected_operation: scope.FileOperation | None = None
                    expected_reason: scope.ScopeInputReason | None = None
                    if old_exists and new_exists:
                        expected_reason = scope.ScopeInputReason.AMBIGUOUS_RENAME_TARGET
                    elif not old_exists and new_exists:
                        if metadata == "PAIR":
                            expected_operation = (
                                scope.FileOperation.NOOP_TARGET_ALREADY_RENAMED
                                if rename_state is RenameContentState.UNCHANGED
                                else scope.FileOperation.TRANSLATE
                            )
                        else:
                            expected_reason = scope.ScopeInputReason.TARGET_PATH_COLLISION
                    elif old_exists and not new_exists:
                        if metadata == "ABS":
                            expected_operation = (
                                scope.FileOperation.RENAME_TARGET
                                if rename_state is RenameContentState.UNCHANGED
                                else scope.FileOperation.RENAME_TARGET_AND_TRANSLATE
                            )
                        else:
                            expected_reason = scope.ScopeInputReason.TARGET_PATH_COLLISION
                    else:
                        expected_operation = scope.FileOperation.TRANSLATE

                    if expected_reason is not None:
                        with pytest.raises(scope.InvalidScopeInput) as caught:
                            scope.build_potential_scopes(
                                _Reader(values),
                                _Scanner({source.path: ()}),
                                preflight,
                                _snapshots(),
                                (inventory,),
                                dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
                            )
                        assert caught.value.reason is expected_reason
                        assert caught.value.direction is direction
                        assert caught.value.path is target.path
                        assert caught.value.args == (
                            f"invalid_scope_input:{expected_reason.value}",
                        )
                        assert preflight.calls == []
                        counts = (
                            collision_counts
                            if expected_reason is scope.ScopeInputReason.TARGET_PATH_COLLISION
                            else ambiguous_counts
                        )
                        counts[direction] += 1
                        attributed_counts[direction] += 1
                        continue

                    result = scope.build_potential_scopes(
                        _Reader(values),
                        _Scanner({source.path: ()}),
                        preflight,
                        _snapshots(),
                        (inventory,),
                        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
                    )
                    directional = next(item for item in result.scopes if item.direction is direction)
                    entry = next(
                        item
                        for item in directional.entries
                        if item.origin is scope.ScopeOrigin.INITIAL
                    )
                    assert entry.pair.source_path is source.path
                    assert entry.pair.target_path is target.path
                    assert entry.origin is scope.ScopeOrigin.INITIAL
                    assert entry.operation is expected_operation
                    assert entry.initial_keys == (inventory.key,)
                    if expected_operation in {
                        scope.FileOperation.TRANSLATE,
                        scope.FileOperation.RENAME_TARGET_AND_TRANSLATE,
                    }:
                        assert entry.source_content is source.content
                    else:
                        assert entry.source_content is None
                    if expected_operation in {
                        scope.FileOperation.NOOP_TARGET_ALREADY_RENAMED,
                        scope.FileOperation.TRANSLATE,
                    }:
                        assert entry.target_content is target.content
                    else:
                        assert entry.target_content is None
                    if expected_operation in {
                        scope.FileOperation.RENAME_TARGET,
                        scope.FileOperation.RENAME_TARGET_AND_TRANSLATE,
                    }:
                        assert entry.rename_from_target_path == old_target
                        assert entry.rename_from_target_content is values[old_target]
                    else:
                        assert entry.rename_from_target_path is None
                        assert entry.rename_from_target_content is None
                    assert len(preflight.calls) == 1
                    operation_counts[direction] += 1
                    attributed_counts[direction] += 1

    assert combined_invalid_count == 4
    assert operation_counts == {Direction.RU_TO_EN: 14, Direction.EN_TO_RU: 14}
    assert collision_counts == {Direction.RU_TO_EN: 20, Direction.EN_TO_RU: 14}
    assert ambiguous_counts == {Direction.RU_TO_EN: 10, Direction.EN_TO_RU: 8}
    assert attributed_counts == {Direction.RU_TO_EN: 44, Direction.EN_TO_RU: 36}


def _undetermined_diagnostic() -> Diagnostic:
    return Diagnostic(
        Severity.YELLOW,
        "direction_undetermined",
        DIRECTION_UNDETERMINED_WARNING,
        DIRECTION_UNDETERMINED_ACTION,
    )


def test_a013_retained_freeze_state_and_identity_matrix() -> None:
    a_key = PairKey(RepoPath("a.md"))
    z_key = PairKey(RepoPath("z.md"))
    selected_pair = _inventory(
        "a.md",
        b"selected",
        None,
        (_change(Locale.RU, ChangedFileKind.ADDED, a_key),),
    )
    complete_pair = _inventory(
        "z.md",
        b"complete",
        b"translated",
        (_change(Locale.RU, ChangedFileKind.MODIFIED, z_key),),
    )
    dependency_path = RepoPath("ydb/docs/ru/dependency.md")
    dependency_target = RepoPath("ydb/docs/en/dependency.md")
    potential = scope.build_potential_scopes(
        _Reader({dependency_path: b"complete-only dependency", dependency_target: None}),
        _Scanner(
            {
                selected_pair.ru.path: (),
                complete_pair.ru.path: (
                    dependencies.DependencyLink(complete_pair.ru.path, dependency_path),
                ),
                dependency_path: (),
            }
        ),
        _Preflight(),
        _snapshots(),
        (selected_pair, complete_pair),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    selected_result = DirectionSelectionResult(
        DirectionSelectionState.SELECTED,
        Direction.RU_TO_EN,
        (
            DirectionPairDecision(selected_pair, DirectionPairVerdict.RU_TO_EN),
            DirectionPairDecision(complete_pair, DirectionPairVerdict.COMPLETE_PAIR),
        ),
        None,
    )
    selected = scope.freeze_scope_manifest(potential, selected_result)
    assert selected.state is scope.ScopeSelectionState.SELECTED
    assert selected.manifest is not None
    assert tuple(outcome.key for outcome in selected.manifest.initial_outcomes) == (a_key, z_key)
    assert tuple(outcome.disposition for outcome in selected.manifest.initial_outcomes) == (
        scope.InitialPairDisposition.SELECTED,
        scope.InitialPairDisposition.COMPLETE_PAIR,
    )
    assert tuple(entry.pair.source_path for entry in selected.manifest.entries) == (
        selected_pair.ru.path,
    )
    assert selected.manifest.dependency_file_count == 0
    assert selected.manifest.source_character_count == len("selected")

    complete_potential = scope.build_potential_scopes(
        _Reader({}),
        _Scanner({complete_pair.ru.path: ()}),
        _Preflight(),
        _snapshots(),
        (complete_pair,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    no_translate_result = DirectionSelectionResult(
        DirectionSelectionState.NO_TRANSLATE,
        None,
        (DirectionPairDecision(complete_pair, DirectionPairVerdict.COMPLETE_PAIR),),
        None,
    )
    assert scope.freeze_scope_manifest(complete_potential, no_translate_result) == scope.ScopeSelection(
        scope.ScopeSelectionState.NO_TRANSLATE, None
    )

    undetermined_result = DirectionSelectionResult(
        DirectionSelectionState.DIRECTION_UNDETERMINED,
        None,
        (DirectionPairDecision(selected_pair, DirectionPairVerdict.UNDETERMINED),),
        _undetermined_diagnostic(),
    )
    selected_only_potential = scope.build_potential_scopes(
        _Reader({}),
        _Scanner({selected_pair.ru.path: ()}),
        _Preflight(),
        _snapshots(),
        (selected_pair,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    assert scope.freeze_scope_manifest(
        selected_only_potential, undetermined_result
    ) == scope.ScopeSelection(scope.ScopeSelectionState.DIRECTION_UNDETERMINED, None)

    equal_selected = _inventory(
        "a.md",
        b"selected",
        None,
        (_change(Locale.RU, ChangedFileKind.ADDED, a_key),),
    )
    equal_complete = _inventory(
        "z.md",
        b"complete",
        b"translated",
        (_change(Locale.RU, ChangedFileKind.MODIFIED, z_key),),
    )
    identity_cases = (
        (
            selected_only_potential,
            DirectionSelectionResult(
                DirectionSelectionState.SELECTED,
                Direction.RU_TO_EN,
                (DirectionPairDecision(equal_selected, DirectionPairVerdict.RU_TO_EN),),
                None,
            ),
            Direction.RU_TO_EN,
        ),
        (
            complete_potential,
            DirectionSelectionResult(
                DirectionSelectionState.NO_TRANSLATE,
                None,
                (DirectionPairDecision(equal_complete, DirectionPairVerdict.COMPLETE_PAIR),),
                None,
            ),
            None,
        ),
        (
            selected_only_potential,
            DirectionSelectionResult(
                DirectionSelectionState.DIRECTION_UNDETERMINED,
                None,
                (DirectionPairDecision(equal_selected, DirectionPairVerdict.UNDETERMINED),),
                _undetermined_diagnostic(),
            ),
            None,
        ),
    )
    for current_potential, result, expected_direction in identity_cases:
        with pytest.raises(scope.InvalidScopeInput) as caught:
            scope.freeze_scope_manifest(current_potential, result)
        assert caught.value.reason is scope.ScopeInputReason.DIRECTION_RESULT_MISMATCH
        assert caught.value.direction is expected_direction
        assert caught.value.path is None
        assert caught.value.args == ("invalid_scope_input:direction_result_mismatch",)

    empty_potential = scope.build_potential_scopes(
        _Reader({}),
        _Scanner({}),
        _Preflight(),
        _snapshots(),
        (),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )
    empty_valid = DirectionSelectionResult(DirectionSelectionState.NO_TRANSLATE, None, (), None)
    assert scope.freeze_scope_manifest(empty_potential, empty_valid) == scope.ScopeSelection(
        scope.ScopeSelectionState.NO_TRANSLATE, None
    )
    with pytest.raises(scope.InvalidScopeInput) as caught:
        scope.freeze_scope_manifest(empty_potential, no_translate_result)
    assert caught.value.reason is scope.ScopeInputReason.DIRECTION_RESULT_MISMATCH

    signature = inspect.signature(scope.freeze_scope_manifest)
    assert tuple(signature.parameters) == ("potential", "direction_result")
    assert all(
        forbidden not in signature.parameters
        for forbidden in ("reader", "scanner", "preflight", "model", "mutation")
    )


def test_existing_target_adds_missing_symmetric_linked_article_to_scope() -> None:
    key = PairKey(RepoPath("changelog.md"))
    parent = _inventory(
        "changelog.md",
        b"See [hints](optimization/hints.md).",
        b"See [hints](query-optimization/query-hints.md).",
        (_change(Locale.RU, ChangedFileKind.MODIFIED, key),),
    )
    dependency_path = RepoPath("ydb/docs/ru/optimization/hints.md")
    dependency_target = RepoPath("ydb/docs/en/optimization/hints.md")
    potential = scope.build_potential_scopes(
        _Reader({dependency_path: b"# Hints\n", dependency_target: None}),
        _Scanner(
            {
                parent.ru.path: (
                    dependencies.DependencyLink(parent.ru.path, dependency_path),
                ),
                dependency_path: (),
            }
        ),
        _Preflight(),
        _snapshots(),
        (parent,),
        dependencies.RedirectCatalog(_snapshot(), _roots(), ()),
    )

    selected = scope.freeze_scope_manifest(
        potential,
        DirectionSelectionResult(
            DirectionSelectionState.SELECTED,
            Direction.RU_TO_EN,
            (DirectionPairDecision(parent, DirectionPairVerdict.RU_TO_EN),),
            None,
        ),
    )

    assert selected.manifest is not None
    assert tuple(entry.pair.source_path for entry in selected.manifest.entries) == (
        parent.ru.path,
        dependency_path,
    )
    dependency = selected.manifest.entries[1]
    assert dependency.origin is scope.ScopeOrigin.DEPENDENCY
    assert dependency.pair.target_path == dependency_target
    assert dependency.target_content is None
    assert selected.manifest.dependency_file_count == 1
