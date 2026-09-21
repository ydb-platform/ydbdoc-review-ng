"""Provider-neutral direction selection over immutable locale inventories."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ydbdoc_review_ng.domain import (
    Diagnostic,
    Locale,
    ModelRole,
    RepoPath,
    Severity,
    SnapshotRef,
)
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.locales import LocalePairInventory, PairFileState, PairKey
from ydbdoc_review_ng.ports import ModelClient

__all__ = (
    "DIRECTION_UNDETERMINED_ACTION",
    "DIRECTION_UNDETERMINED_WARNING",
    "Direction",
    "DirectionInputReason",
    "DirectionModelDecision",
    "DirectionModelPair",
    "DirectionModelRequest",
    "DirectionModelResponse",
    "DirectionPairDecision",
    "DirectionPairVerdict",
    "DirectionResponseReason",
    "DirectionSelectionError",
    "DirectionSelectionResult",
    "DirectionSelectionState",
    "InvalidDirectionInput",
    "InvalidDirectionResponse",
    "select_direction",
)

DIRECTION_UNDETERMINED_WARNING = (
    "Автоматический перевод не запущен: не удалось определить направление перевода"
)
DIRECTION_UNDETERMINED_ACTION = (
    "Уточните исходные изменения и запустите новый `doc_translate`. "
    "Для проверки исправленной translation branch используйте `doc_verify`."
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


def _key_value(key: PairKey) -> str:
    return key.relative_path.value


class Direction(str, Enum):
    RU_TO_EN = "ru_to_en"
    EN_TO_RU = "en_to_ru"


class DirectionSelectionState(str, Enum):
    SELECTED = "selected"
    DIRECTION_UNDETERMINED = "direction_undetermined"
    NO_TRANSLATE = "no_translate"


class DirectionPairVerdict(str, Enum):
    COMPLETE_PAIR = "complete_pair"
    RU_TO_EN = "ru_to_en"
    EN_TO_RU = "en_to_ru"
    UNDETERMINED = "undetermined"


class DirectionInputReason(str, Enum):
    DUPLICATE_PAIR_KEY = "duplicate_pair_key"
    EMPTY_PAIR_CHANGES = "empty_pair_changes"
    MIXED_ROOTS = "mixed_roots"
    MIXED_SNAPSHOTS = "mixed_snapshots"


class DirectionResponseReason(str, Enum):
    WRONG_RESPONSE_TYPE = "wrong_response_type"
    KEY_SET_MISMATCH = "key_set_mismatch"
    COMPLETE_PAIR_REQUIRES_BOTH_PRESENT = "complete_pair_requires_both_present"


class DirectionSelectionError(ValueError):
    """Base class for typed direction-selection operation errors."""


class InvalidDirectionInput(DirectionSelectionError):
    reason: DirectionInputReason
    key: PairKey | None

    __slots__ = ("key", "reason")

    def __init__(self, reason: DirectionInputReason, key: PairKey | None, /) -> None:
        _exact(reason, DirectionInputReason, "InvalidDirectionInput", "reason")
        _optional_exact(key, PairKey, "InvalidDirectionInput", "key")
        self.reason = reason
        self.key = key
        super().__init__(f"invalid_direction_input:{reason.value}")


class InvalidDirectionResponse(DirectionSelectionError):
    reason: DirectionResponseReason
    key: PairKey | None

    __slots__ = ("key", "reason")

    def __init__(self, reason: DirectionResponseReason, key: PairKey | None, /) -> None:
        _exact(reason, DirectionResponseReason, "InvalidDirectionResponse", "reason")
        _optional_exact(key, PairKey, "InvalidDirectionResponse", "key")
        self.reason = reason
        self.key = key
        super().__init__(f"invalid_direction_response:{reason.value}")


@dataclass(frozen=True, slots=True)
class DirectionModelPair:
    key: PairKey
    snapshot: SnapshotRef
    ru_path: RepoPath
    ru_content: bytes | None = field(repr=False)
    en_path: RepoPath
    en_content: bytes | None = field(repr=False)

    def __post_init__(self) -> None:
        type_name = "DirectionModelPair"
        _exact(self.key, PairKey, type_name, "key")
        _exact(self.snapshot, SnapshotRef, type_name, "snapshot")
        _exact(self.ru_path, RepoPath, type_name, "ru_path")
        if self.ru_content is not None and type(self.ru_content) is not bytes:
            raise _invariant(type_name, "ru_content", "None or exact bytes")
        _exact(self.en_path, RepoPath, type_name, "en_path")
        if self.en_content is not None and type(self.en_content) is not bytes:
            raise _invariant(type_name, "en_content", "None or exact bytes")


@dataclass(frozen=True, slots=True)
class DirectionModelRequest:
    role: ModelRole
    pairs: tuple[DirectionModelPair, ...]
    operator_context: str | None = field(repr=False)

    def __post_init__(self) -> None:
        type_name = "DirectionModelRequest"
        _exact(self.role, ModelRole, type_name, "role")
        _exact(self.pairs, tuple, type_name, "pairs")
        _optional_exact(self.operator_context, str, type_name, "operator_context")
        for pair in self.pairs:
            _exact(pair, DirectionModelPair, type_name, "pairs")
        if self.role is not ModelRole.DIRECTION:
            raise _invariant(type_name, "role", "ModelRole.DIRECTION")
        if not self.pairs:
            raise _invariant(type_name, "pairs", "a non-empty canonical tuple")
        keys = tuple(_key_value(pair.key) for pair in self.pairs)
        if keys != tuple(sorted(set(keys))):
            raise _invariant(type_name, "pairs", "unique key-sorted pairs")
        if len({pair.snapshot for pair in self.pairs}) != 1:
            raise _invariant(type_name, "pairs", "one snapshot")
        if self.operator_context is not None and not self.operator_context.strip():
            raise _invariant(type_name, "operator_context", "None or a non-empty string")


@dataclass(frozen=True, slots=True)
class DirectionModelDecision:
    key: PairKey
    verdict: DirectionPairVerdict

    def __post_init__(self) -> None:
        _exact(self.key, PairKey, "DirectionModelDecision", "key")
        _exact(self.verdict, DirectionPairVerdict, "DirectionModelDecision", "verdict")


@dataclass(frozen=True, slots=True)
class DirectionPairDecision:
    pair: LocalePairInventory = field(repr=False)
    verdict: DirectionPairVerdict

    def __post_init__(self) -> None:
        _exact(self.pair, LocalePairInventory, "DirectionPairDecision", "pair")
        _exact(self.verdict, DirectionPairVerdict, "DirectionPairDecision", "verdict")


@dataclass(frozen=True, slots=True)
class DirectionModelResponse:
    decisions: tuple[DirectionModelDecision, ...]

    def __post_init__(self) -> None:
        type_name = "DirectionModelResponse"
        _exact(self.decisions, tuple, type_name, "decisions")
        for decision in self.decisions:
            _exact(decision, DirectionModelDecision, type_name, "decisions")
        if not self.decisions:
            raise _invariant(type_name, "decisions", "a non-empty canonical tuple")
        keys = tuple(_key_value(decision.key) for decision in self.decisions)
        if keys != tuple(sorted(set(keys))):
            raise _invariant(type_name, "decisions", "unique key-sorted decisions")


def _undetermined_diagnostic() -> Diagnostic:
    return Diagnostic(
        Severity.YELLOW,
        "direction_undetermined",
        DIRECTION_UNDETERMINED_WARNING,
        DIRECTION_UNDETERMINED_ACTION,
    )


@dataclass(frozen=True, slots=True)
class DirectionSelectionResult:
    state: DirectionSelectionState
    direction: Direction | None
    decisions: tuple[DirectionPairDecision, ...]
    diagnostic: Diagnostic | None

    def __post_init__(self) -> None:
        type_name = "DirectionSelectionResult"
        _exact(self.state, DirectionSelectionState, type_name, "state")
        _optional_exact(self.direction, Direction, type_name, "direction")
        _exact(self.decisions, tuple, type_name, "decisions")
        _optional_exact(self.diagnostic, Diagnostic, type_name, "diagnostic")
        for decision in self.decisions:
            _exact(decision, DirectionPairDecision, type_name, "decisions")

        keys = tuple(_key_value(decision.pair.key) for decision in self.decisions)
        if keys != tuple(sorted(set(keys))):
            raise _invariant(type_name, "decisions", "unique key-sorted decisions")
        if any(not decision.pair.changes for decision in self.decisions):
            raise _invariant(type_name, "decisions", "inventories with non-empty changes")
        if self.decisions:
            roots = self.decisions[0].pair.roots
            snapshot = self.decisions[0].pair.ru.snapshot
            if any(decision.pair.roots != roots for decision in self.decisions):
                raise _invariant(type_name, "decisions", "one locale-roots value")
            if any(decision.pair.ru.snapshot != snapshot for decision in self.decisions):
                raise _invariant(type_name, "decisions", "one snapshot")
        if any(
            decision.verdict is DirectionPairVerdict.COMPLETE_PAIR
            and decision.pair.state is not PairFileState.BOTH_PRESENT
            for decision in self.decisions
        ):
            raise _invariant(type_name, "decisions", "complete verdicts only for both-present pairs")

        non_complete = tuple(
            decision
            for decision in self.decisions
            if decision.verdict is not DirectionPairVerdict.COMPLETE_PAIR
        )
        directional = {
            decision.verdict
            for decision in non_complete
            if decision.verdict
            in {DirectionPairVerdict.RU_TO_EN, DirectionPairVerdict.EN_TO_RU}
        }
        has_undetermined = any(
            decision.verdict is DirectionPairVerdict.UNDETERMINED for decision in non_complete
        )

        if self.state is DirectionSelectionState.SELECTED:
            if self.direction is None:
                raise _invariant(type_name, "direction", "a direction for SELECTED")
            if self.diagnostic is not None:
                raise _invariant(type_name, "diagnostic", "None for SELECTED")
            expected = (
                DirectionPairVerdict.RU_TO_EN
                if self.direction is Direction.RU_TO_EN
                else DirectionPairVerdict.EN_TO_RU
            )
            if not non_complete or has_undetermined or any(
                decision.verdict is not expected for decision in non_complete
            ):
                raise _invariant(type_name, "decisions", "non-complete decisions matching direction")
        elif self.state is DirectionSelectionState.NO_TRANSLATE:
            if self.direction is not None:
                raise _invariant(type_name, "direction", "None for NO_TRANSLATE")
            if self.diagnostic is not None:
                raise _invariant(type_name, "diagnostic", "None for NO_TRANSLATE")
            if non_complete:
                raise _invariant(type_name, "decisions", "only complete decisions")
        else:
            if self.direction is not None:
                raise _invariant(type_name, "direction", "None for DIRECTION_UNDETERMINED")
            if not has_undetermined:
                raise _invariant(type_name, "decisions", "at least one undetermined decision")
            if self.diagnostic != _undetermined_diagnostic():
                raise _invariant(type_name, "diagnostic", "the direction-undetermined diagnostic")
            if len(directional) > 1:
                raise _invariant(type_name, "decisions", "at most one retained direction")

    @property
    def complete_pairs(self) -> tuple[LocalePairInventory, ...]:
        return tuple(
            decision.pair
            for decision in self.decisions
            if decision.verdict is DirectionPairVerdict.COMPLETE_PAIR
        )

    @property
    def selected_pairs(self) -> tuple[LocalePairInventory, ...]:
        if self.state is not DirectionSelectionState.SELECTED:
            return ()
        return tuple(
            decision.pair
            for decision in self.decisions
            if decision.verdict is not DirectionPairVerdict.COMPLETE_PAIR
        )

    @property
    def unresolved_pairs(self) -> tuple[LocalePairInventory, ...]:
        if self.state is not DirectionSelectionState.DIRECTION_UNDETERMINED:
            return ()
        return tuple(
            decision.pair
            for decision in self.decisions
            if decision.verdict is DirectionPairVerdict.UNDETERMINED
        )


def _project(pair: LocalePairInventory) -> DirectionModelPair:
    return DirectionModelPair(
        pair.key,
        pair.ru.snapshot,
        pair.ru.path,
        pair.ru.content,
        pair.en.path,
        pair.en.content,
    )


def _validate_inventories(
    inventories: tuple[LocalePairInventory, ...],
) -> tuple[LocalePairInventory, ...]:
    _exact(inventories, tuple, "select_direction", "inventories")
    for pair in inventories:
        _exact(pair, LocalePairInventory, "select_direction", "inventories")

    counts: dict[PairKey, int] = {}
    for pair in inventories:
        counts[pair.key] = counts.get(pair.key, 0) + 1
    duplicates = [key for key, count in counts.items() if count > 1]
    if duplicates:
        raise InvalidDirectionInput(
            DirectionInputReason.DUPLICATE_PAIR_KEY, min(duplicates, key=_key_value)
        )
    empty = [pair.key for pair in inventories if not pair.changes]
    if empty:
        raise InvalidDirectionInput(
            DirectionInputReason.EMPTY_PAIR_CHANGES, min(empty, key=_key_value)
        )
    if inventories:
        canonical_roots = min(
            (pair.roots for pair in inventories), key=lambda roots: (roots.ru.value, roots.en.value)
        )
        wrong_roots = [pair.key for pair in inventories if pair.roots != canonical_roots]
        if wrong_roots:
            raise InvalidDirectionInput(
                DirectionInputReason.MIXED_ROOTS, min(wrong_roots, key=_key_value)
            )
        canonical_snapshot = min(
            (pair.ru.snapshot for pair in inventories),
            key=lambda snapshot: (snapshot.repository.value, snapshot.commit_sha.value),
        )
        wrong_snapshots = [
            pair.key for pair in inventories if pair.ru.snapshot != canonical_snapshot
        ]
        if wrong_snapshots:
            raise InvalidDirectionInput(
                DirectionInputReason.MIXED_SNAPSHOTS,
                min(wrong_snapshots, key=_key_value),
            )
    return tuple(sorted(inventories, key=lambda pair: _key_value(pair.key)))


def _validate_response(
    response: object, requested: tuple[LocalePairInventory, ...]
) -> tuple[DirectionPairDecision, ...]:
    if type(response) is not DirectionModelResponse:
        raise InvalidDirectionResponse(DirectionResponseReason.WRONG_RESPONSE_TYPE, None)
    requested_by_key = {pair.key: pair for pair in requested}
    returned_by_key = {decision.key: decision for decision in response.decisions}
    difference = set(requested_by_key) ^ set(returned_by_key)
    if difference:
        raise InvalidDirectionResponse(
            DirectionResponseReason.KEY_SET_MISMATCH, min(difference, key=_key_value)
        )
    invalid_complete = [
        decision.key
        for decision in response.decisions
        if decision.verdict is DirectionPairVerdict.COMPLETE_PAIR
        and requested_by_key[decision.key].state is not PairFileState.BOTH_PRESENT
    ]
    if invalid_complete:
        raise InvalidDirectionResponse(
            DirectionResponseReason.COMPLETE_PAIR_REQUIRES_BOTH_PRESENT,
            min(invalid_complete, key=_key_value),
        )
    return tuple(
        DirectionPairDecision(requested_by_key[decision.key], decision.verdict)
        for decision in response.decisions
    )


def _aggregate(
    decisions: tuple[DirectionPairDecision, ...],
) -> DirectionSelectionResult:
    non_complete = tuple(
        decision
        for decision in decisions
        if decision.verdict is not DirectionPairVerdict.COMPLETE_PAIR
    )
    if not non_complete:
        return DirectionSelectionResult(
            DirectionSelectionState.NO_TRANSLATE, None, decisions, None
        )
    directional = {
        decision.verdict
        for decision in non_complete
        if decision.verdict
        in {DirectionPairVerdict.RU_TO_EN, DirectionPairVerdict.EN_TO_RU}
    }
    has_undetermined = any(
        decision.verdict is DirectionPairVerdict.UNDETERMINED for decision in non_complete
    )
    if len(directional) == 1 and not has_undetermined:
        verdict = next(iter(directional))
        direction = (
            Direction.RU_TO_EN
            if verdict is DirectionPairVerdict.RU_TO_EN
            else Direction.EN_TO_RU
        )
        return DirectionSelectionResult(
            DirectionSelectionState.SELECTED, direction, decisions, None
        )
    if len(directional) > 1:
        decisions = tuple(
            decision
            if decision.verdict is DirectionPairVerdict.COMPLETE_PAIR
            else DirectionPairDecision(decision.pair, DirectionPairVerdict.UNDETERMINED)
            for decision in decisions
        )
    return DirectionSelectionResult(
        DirectionSelectionState.DIRECTION_UNDETERMINED,
        None,
        decisions,
        _undetermined_diagnostic(),
    )


def _invoke(
    client: ModelClient[DirectionModelRequest, DirectionModelResponse],
    inventories: tuple[LocalePairInventory, ...],
    operator_context: str | None,
) -> tuple[DirectionPairDecision, ...]:
    request = DirectionModelRequest(
        ModelRole.DIRECTION,
        tuple(_project(pair) for pair in inventories),
        operator_context,
    )
    response = client.invoke(request)
    return _validate_response(response, inventories)


def select_direction(
    client: ModelClient[DirectionModelRequest, DirectionModelResponse],
    inventories: tuple[LocalePairInventory, ...],
    /,
) -> DirectionSelectionResult:
    canonical = _validate_inventories(inventories)
    if not canonical:
        return DirectionSelectionResult(
            DirectionSelectionState.NO_TRANSLATE, None, (), None
        )
    changed_locales = {
        locale for pair in canonical for locale in pair.changed_locales
    }
    if changed_locales == {Locale.RU}:
        return _aggregate(
            tuple(
                DirectionPairDecision(pair, DirectionPairVerdict.RU_TO_EN)
                for pair in canonical
            )
        )
    if changed_locales == {Locale.EN}:
        return _aggregate(
            tuple(
                DirectionPairDecision(pair, DirectionPairVerdict.EN_TO_RU)
                for pair in canonical
            )
        )
    return _aggregate(_invoke(client, canonical, None))
