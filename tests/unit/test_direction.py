from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import cast

import pytest

from ydbdoc_review_ng.direction import (
    DIRECTION_UNDETERMINED_ACTION,
    DIRECTION_UNDETERMINED_WARNING,
    Direction,
    DirectionInputReason,
    DirectionModelDecision,
    DirectionModelPair,
    DirectionModelRequest,
    DirectionModelResponse,
    DirectionPairDecision,
    DirectionPairVerdict,
    DirectionResponseReason,
    DirectionSelectionError,
    DirectionSelectionResult,
    DirectionSelectionState,
    InvalidDirectionInput,
    InvalidDirectionResponse,
    select_direction,
)
from ydbdoc_review_ng.domain import (
    Diagnostic,
    GitSha,
    Locale,
    ModelRole,
    RepoPath,
    RepositoryId,
    Severity,
    SnapshotRef,
    to_wire,
)
from ydbdoc_review_ng.errors import InvariantViolation, UnknownDomainType
from ydbdoc_review_ng.locales import (
    ChangedFileKind,
    ChangedMarkdownFile,
    LocalePairInventory,
    LocaleRoots,
    PairFileState,
    PairKey,
    SnapshotLocaleFile,
)

PUBLIC = (
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

ROOTS = LocaleRoots(RepoPath("ydb/docs/ru"), RepoPath("ydb/docs/en"))
OTHER_ROOTS = LocaleRoots(RepoPath("docs/ru"), RepoPath("docs/en"))
SNAPSHOT = SnapshotRef(
    RepositoryId("ydb-platform/ydb"),
    GitSha("5aab6d0e65926540eff571d03a018a492face7e1"),
)
OTHER_SNAPSHOT = SnapshotRef(
    RepositoryId("ydb-platform/ydb"),
    GitSha("a" * 40),
)
CANARY_BYTES = b"TOP-SECRET-DOCUMENT-CANARY"
CANARY_CONTEXT = "  first line\nTOP-SECRET-CONTEXT-CANARY\n"


def _path(roots: LocaleRoots, locale: Locale, key: PairKey) -> RepoPath:
    root = roots.ru if locale is Locale.RU else roots.en
    return RepoPath(f"{root.value}/{key.relative_path.value}")


def _change(roots: LocaleRoots, locale: Locale, key: PairKey) -> ChangedMarkdownFile:
    path = _path(roots, locale, key)
    return ChangedMarkdownFile(
        roots,
        ChangedFileKind.MODIFIED,
        locale,
        key,
        path,
        path,
        None,
        None,
    )


def inventory(
    name: str,
    *,
    changed: tuple[Locale, ...] = (Locale.RU,),
    state: PairFileState = PairFileState.BOTH_PRESENT,
    roots: LocaleRoots = ROOTS,
    snapshot: SnapshotRef = SNAPSHOT,
    ru_content: bytes | None = b"RU",
    en_content: bytes | None = b"EN",
) -> LocalePairInventory:
    key = PairKey(RepoPath(name))
    if state is PairFileState.RU_ONLY:
        en_content = None
        if ru_content is None:
            ru_content = b"RU"
    elif state is PairFileState.EN_ONLY:
        ru_content = None
        if en_content is None:
            en_content = b"EN"
    elif state is PairFileState.BOTH_MISSING:
        ru_content = None
        en_content = None
    elif state is PairFileState.BOTH_PRESENT:
        if ru_content is None:
            ru_content = b"RU"
        if en_content is None:
            en_content = b"EN"
    ru = SnapshotLocaleFile(
        roots, Locale.RU, key, _path(roots, Locale.RU, key), snapshot, ru_content
    )
    en = SnapshotLocaleFile(
        roots, Locale.EN, key, _path(roots, Locale.EN, key), snapshot, en_content
    )
    changes = tuple(_change(roots, locale, key) for locale in changed)
    return LocalePairInventory(roots, key, ru, en, changes, state)


def model_response(
    *items: tuple[LocalePairInventory, DirectionPairVerdict],
) -> DirectionModelResponse:
    decisions = tuple(
        DirectionModelDecision(pair.key, verdict)
        for pair, verdict in sorted(items, key=lambda item: item[0].key.relative_path.value)
    )
    return DirectionModelResponse(decisions)


class FakeClient:
    def __init__(
        self,
        response: object | Callable[[DirectionModelRequest], object],
        *,
        fail_on_second: bool = False,
    ) -> None:
        self.response = response
        self.fail_on_second = fail_on_second
        self.calls: list[DirectionModelRequest] = []

    def invoke(self, request: DirectionModelRequest, /) -> DirectionModelResponse:
        self.calls.append(request)
        if self.fail_on_second and len(self.calls) > 1:
            raise AssertionError("second invocation")
        result = self.response(request) if callable(self.response) else self.response
        return cast(DirectionModelResponse, result)


def undecided_result(
    *items: tuple[LocalePairInventory, DirectionPairVerdict],
) -> DirectionSelectionResult:
    decisions = tuple(
        DirectionPairDecision(pair, verdict)
        for pair, verdict in sorted(items, key=lambda item: item[0].key.relative_path.value)
    )
    return DirectionSelectionResult(
        DirectionSelectionState.DIRECTION_UNDETERMINED,
        None,
        decisions,
        Diagnostic(
            Severity.YELLOW,
            "direction_undetermined",
            DIRECTION_UNDETERMINED_WARNING,
            DIRECTION_UNDETERMINED_ACTION,
        ),
    )


def assert_safe_direction_error(error: ValueError) -> None:
    combined = f"{error.args!r} {error!s} {error!r}"
    assert "CANARY" not in combined
    assert "ydb/docs" not in combined


def test_a002_exact_public_api_enums_records_and_signatures() -> None:
    import ydbdoc_review_ng.direction as module

    assert module.__all__ == PUBLIC
    assert tuple((item.name, item.value) for item in Direction) == (
        ("RU_TO_EN", "ru_to_en"),
        ("EN_TO_RU", "en_to_ru"),
    )
    assert tuple((item.name, item.value) for item in DirectionSelectionState) == (
        ("SELECTED", "selected"),
        ("DIRECTION_UNDETERMINED", "direction_undetermined"),
        ("NO_TRANSLATE", "no_translate"),
    )
    assert tuple((item.name, item.value) for item in DirectionPairVerdict) == (
        ("COMPLETE_PAIR", "complete_pair"),
        ("RU_TO_EN", "ru_to_en"),
        ("EN_TO_RU", "en_to_ru"),
        ("UNDETERMINED", "undetermined"),
    )
    assert tuple((item.name, item.value) for item in DirectionInputReason) == (
        ("DUPLICATE_PAIR_KEY", "duplicate_pair_key"),
        ("EMPTY_PAIR_CHANGES", "empty_pair_changes"),
        ("MIXED_ROOTS", "mixed_roots"),
        ("MIXED_SNAPSHOTS", "mixed_snapshots"),
    )
    assert tuple((item.name, item.value) for item in DirectionResponseReason) == (
        ("WRONG_RESPONSE_TYPE", "wrong_response_type"),
        ("KEY_SET_MISMATCH", "key_set_mismatch"),
        (
            "COMPLETE_PAIR_REQUIRES_BOTH_PRESENT",
            "complete_pair_requires_both_present",
        ),
    )
    assert issubclass(DirectionSelectionError, ValueError)
    assert issubclass(InvalidDirectionInput, DirectionSelectionError)
    assert issubclass(InvalidDirectionResponse, DirectionSelectionError)

    assert [field.name for field in fields(DirectionModelPair)] == [
        "key", "snapshot", "ru_path", "ru_content", "en_path", "en_content"
    ]
    assert [field.name for field in fields(DirectionModelRequest)] == [
        "role", "pairs", "operator_context"
    ]
    assert [field.name for field in fields(DirectionModelDecision)] == ["key", "verdict"]
    assert [field.name for field in fields(DirectionPairDecision)] == ["pair", "verdict"]
    assert [field.name for field in fields(DirectionModelResponse)] == ["decisions"]
    assert [field.name for field in fields(DirectionSelectionResult)] == [
        "state", "direction", "decisions", "diagnostic"
    ]
    assert fields(DirectionModelPair)[3].repr is False
    assert fields(DirectionModelPair)[5].repr is False
    assert fields(DirectionModelRequest)[2].repr is False
    assert fields(DirectionPairDecision)[0].repr is False

    select = inspect.signature(select_direction)
    assert tuple(parameter.kind for parameter in select.parameters.values()) == (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_ONLY,
    )


def test_a003_records_are_frozen_and_hide_content_and_context() -> None:
    pair = inventory("canary.md", ru_content=CANARY_BYTES, en_content=b"")
    projected = DirectionModelPair(
        pair.key,
        SNAPSHOT,
        pair.ru.path,
        pair.ru.content,
        pair.en.path,
        pair.en.content,
    )
    request = DirectionModelRequest(ModelRole.DIRECTION, (projected,), CANARY_CONTEXT)
    decision = DirectionPairDecision(pair, DirectionPairVerdict.RU_TO_EN)
    result = DirectionSelectionResult(
        DirectionSelectionState.SELECTED, Direction.RU_TO_EN, (decision,), None
    )
    assert "CANARY" not in repr(projected)
    assert "CANARY" not in repr(request)
    assert "CANARY" not in repr(decision)
    assert "CANARY" not in repr(result)
    for value, field_name in (
        (projected, "key"),
        (request, "role"),
        (DirectionModelDecision(pair.key, DirectionPairVerdict.RU_TO_EN), "key"),
        (decision, "verdict"),
        (DirectionModelResponse((DirectionModelDecision(pair.key, DirectionPairVerdict.RU_TO_EN),)), "decisions"),
        (result, "state"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field_name, None)


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (lambda: DirectionModelPair("key", SNAPSHOT, RepoPath("a.md"), b"", RepoPath("b.md"), b""), "key"),
        (lambda: DirectionModelPair(PairKey(RepoPath("a.md")), "snapshot", RepoPath("a.md"), b"", RepoPath("b.md"), b""), "snapshot"),
        (lambda: DirectionModelPair(PairKey(RepoPath("a.md")), SNAPSHOT, "a.md", b"", RepoPath("b.md"), b""), "ru_path"),
        (lambda: DirectionModelPair(PairKey(RepoPath("a.md")), SNAPSHOT, RepoPath("a.md"), bytearray(), RepoPath("b.md"), b""), "ru_content"),
        (lambda: DirectionModelPair(PairKey(RepoPath("a.md")), SNAPSHOT, RepoPath("a.md"), b"", "b.md", b""), "en_path"),
        (lambda: DirectionModelPair(PairKey(RepoPath("a.md")), SNAPSHOT, RepoPath("a.md"), b"", RepoPath("b.md"), bytearray()), "en_content"),
        (lambda: DirectionModelRequest("direction", (), None), "role"),
        (lambda: DirectionModelRequest(ModelRole.DIRECTION, [], None), "pairs"),
        (lambda: DirectionModelRequest(ModelRole.DIRECTION, (), 1), "operator_context"),
        (lambda: DirectionModelDecision("key", DirectionPairVerdict.RU_TO_EN), "key"),
        (lambda: DirectionModelDecision(PairKey(RepoPath("a.md")), "ru_to_en"), "verdict"),
        (lambda: DirectionPairDecision("pair", DirectionPairVerdict.RU_TO_EN), "pair"),
        (lambda: DirectionPairDecision(inventory("a.md"), "ru_to_en"), "verdict"),
        (lambda: DirectionModelResponse([]), "decisions"),
        (lambda: DirectionSelectionResult("selected", None, (), None), "state"),
        (lambda: DirectionSelectionResult(DirectionSelectionState.NO_TRANSLATE, "ru_to_en", (), None), "direction"),
        (lambda: DirectionSelectionResult(DirectionSelectionState.NO_TRANSLATE, None, [], None), "decisions"),
        (lambda: DirectionSelectionResult(DirectionSelectionState.NO_TRANSLATE, None, (), "diagnostic"), "diagnostic"),
    ],
)
def test_a003_every_record_field_rejects_wrong_exact_type(
    factory: Callable[[], object], field_name: str
) -> None:
    with pytest.raises(InvariantViolation, match=rf"\.{field_name}:"):
        factory()


def test_a003_record_content_invariants_and_positive_state_matrix() -> None:
    a = inventory("a.md", changed=(Locale.RU, Locale.EN))
    b = inventory("b.md", changed=(Locale.RU, Locale.EN))
    c = inventory("c.md", changed=(Locale.RU, Locale.EN))
    pa = DirectionModelPair(a.key, SNAPSHOT, a.ru.path, a.ru.content, a.en.path, a.en.content)
    pb = DirectionModelPair(b.key, SNAPSHOT, b.ru.path, b.ru.content, b.en.path, b.en.content)
    DirectionModelRequest(ModelRole.DIRECTION, (pa, pb), None)
    DirectionModelRequest(ModelRole.DIRECTION, (pa,), CANARY_CONTEXT)
    with pytest.raises(InvariantViolation, match="DirectionModelRequest.role"):
        DirectionModelRequest(ModelRole.TRANSLATE, (pa,), None)
    with pytest.raises(InvariantViolation, match="DirectionModelRequest.pairs"):
        DirectionModelRequest(ModelRole.DIRECTION, (), None)
    with pytest.raises(InvariantViolation, match="DirectionModelRequest.pairs"):
        DirectionModelRequest(ModelRole.DIRECTION, (pb, pa), None)
    with pytest.raises(InvariantViolation, match="DirectionModelRequest.pairs"):
        DirectionModelRequest(ModelRole.DIRECTION, (pa, pa), None)
    with pytest.raises(InvariantViolation, match="DirectionModelRequest.operator_context"):
        DirectionModelRequest(ModelRole.DIRECTION, (pa,), " \n")
    other = inventory("c.md", snapshot=OTHER_SNAPSHOT)
    pc = DirectionModelPair(
        other.key,
        OTHER_SNAPSHOT,
        other.ru.path,
        other.ru.content,
        other.en.path,
        other.en.content,
    )
    with pytest.raises(InvariantViolation, match="DirectionModelRequest.pairs"):
        DirectionModelRequest(ModelRole.DIRECTION, (pa, pc), None)
    with pytest.raises(InvariantViolation, match="DirectionModelRequest.pairs"):
        DirectionModelRequest(ModelRole.DIRECTION, cast(tuple[DirectionModelPair, ...], (object(),)), None)

    da = DirectionModelDecision(a.key, DirectionPairVerdict.RU_TO_EN)
    db = DirectionModelDecision(b.key, DirectionPairVerdict.EN_TO_RU)
    with pytest.raises(InvariantViolation, match="DirectionModelResponse.decisions"):
        DirectionModelResponse(())
    with pytest.raises(InvariantViolation, match="DirectionModelResponse.decisions"):
        DirectionModelResponse((db, da))
    with pytest.raises(InvariantViolation, match="DirectionModelResponse.decisions"):
        DirectionModelResponse((da, da))
    with pytest.raises(InvariantViolation, match="DirectionModelResponse.decisions"):
        DirectionModelResponse(cast(tuple[DirectionModelDecision, ...], (object(),)))

    empty = DirectionSelectionResult(DirectionSelectionState.NO_TRANSLATE, None, (), None)
    complete = DirectionSelectionResult(
        DirectionSelectionState.NO_TRANSLATE,
        None,
        (DirectionPairDecision(a, DirectionPairVerdict.COMPLETE_PAIR),),
        None,
    )
    selected = DirectionSelectionResult(
        DirectionSelectionState.SELECTED,
        Direction.RU_TO_EN,
        (DirectionPairDecision(a, DirectionPairVerdict.RU_TO_EN),),
        None,
    )
    unresolved = undecided_result((a, DirectionPairVerdict.UNDETERMINED))
    retained = undecided_result(
        (a, DirectionPairVerdict.RU_TO_EN),
        (b, DirectionPairVerdict.UNDETERMINED),
    )
    normalized = undecided_result(
        (a, DirectionPairVerdict.UNDETERMINED),
        (b, DirectionPairVerdict.UNDETERMINED),
    )
    assert empty.decisions == ()
    assert complete.complete_pairs == (a,)
    assert selected.selected_pairs == (a,)
    assert unresolved.unresolved_pairs == (a,)
    assert retained.unresolved_pairs == (b,)
    assert normalized.unresolved_pairs == (a, b)
    assert retained.selected_pairs == ()

    wrong_order = (
        DirectionPairDecision(b, DirectionPairVerdict.RU_TO_EN),
        DirectionPairDecision(a, DirectionPairVerdict.RU_TO_EN),
    )
    with pytest.raises(InvariantViolation, match="DirectionSelectionResult.decisions"):
        DirectionSelectionResult(
            DirectionSelectionState.SELECTED, Direction.RU_TO_EN, wrong_order, None
        )
    with pytest.raises(InvariantViolation, match="DirectionSelectionResult.decisions"):
        DirectionSelectionResult(
            DirectionSelectionState.SELECTED,
            Direction.RU_TO_EN,
            (
                DirectionPairDecision(a, DirectionPairVerdict.RU_TO_EN),
                DirectionPairDecision(a, DirectionPairVerdict.RU_TO_EN),
            ),
            None,
        )
    mixed_roots = inventory("c.md", roots=OTHER_ROOTS)
    with pytest.raises(InvariantViolation, match="DirectionSelectionResult.decisions"):
        DirectionSelectionResult(
            DirectionSelectionState.SELECTED,
            Direction.RU_TO_EN,
            (
                DirectionPairDecision(a, DirectionPairVerdict.RU_TO_EN),
                DirectionPairDecision(mixed_roots, DirectionPairVerdict.RU_TO_EN),
            ),
            None,
        )
    mixed_snapshot = inventory("c.md", snapshot=OTHER_SNAPSHOT)
    with pytest.raises(InvariantViolation, match="DirectionSelectionResult.decisions"):
        DirectionSelectionResult(
            DirectionSelectionState.SELECTED,
            Direction.RU_TO_EN,
            (
                DirectionPairDecision(a, DirectionPairVerdict.RU_TO_EN),
                DirectionPairDecision(mixed_snapshot, DirectionPairVerdict.RU_TO_EN),
            ),
            None,
        )
    no_changes = inventory("c.md", changed=())
    with pytest.raises(InvariantViolation, match="DirectionSelectionResult.decisions"):
        DirectionSelectionResult(
            DirectionSelectionState.SELECTED,
            Direction.RU_TO_EN,
            (DirectionPairDecision(no_changes, DirectionPairVerdict.RU_TO_EN),),
            None,
        )
    one_sided = inventory("c.md", state=PairFileState.RU_ONLY)
    with pytest.raises(InvariantViolation, match="DirectionSelectionResult.decisions"):
        DirectionSelectionResult(
            DirectionSelectionState.NO_TRANSLATE,
            None,
            (DirectionPairDecision(one_sided, DirectionPairVerdict.COMPLETE_PAIR),),
            None,
        )
    with pytest.raises(InvariantViolation, match="DirectionSelectionResult.decisions"):
        undecided_result(
            (a, DirectionPairVerdict.RU_TO_EN),
            (b, DirectionPairVerdict.EN_TO_RU),
            (c, DirectionPairVerdict.UNDETERMINED),
        )
    with pytest.raises(InvariantViolation, match="DirectionSelectionResult.decisions"):
        DirectionSelectionResult(
            DirectionSelectionState.NO_TRANSLATE,
            None,
            cast(tuple[DirectionPairDecision, ...], (object(),)),
            None,
        )


def test_a003_exception_constructors_are_exact_and_non_echoing() -> None:
    pair = inventory("secret-path.md")
    for error in (
        InvalidDirectionInput(DirectionInputReason.DUPLICATE_PAIR_KEY, pair.key),
        InvalidDirectionResponse(DirectionResponseReason.KEY_SET_MISMATCH, pair.key),
    ):
        assert_safe_direction_error(error)
        assert error.args == (
            f"{'invalid_direction_input' if isinstance(error, InvalidDirectionInput) else 'invalid_direction_response'}:{error.reason.value}",
        )
    with pytest.raises(InvariantViolation, match="InvalidDirectionInput.reason"):
        InvalidDirectionInput("duplicate_pair_key", None)
    with pytest.raises(InvariantViolation, match="InvalidDirectionInput.key"):
        InvalidDirectionInput(DirectionInputReason.DUPLICATE_PAIR_KEY, "key")
    with pytest.raises(InvariantViolation, match="InvalidDirectionResponse.reason"):
        InvalidDirectionResponse("key_set_mismatch", None)
    with pytest.raises(InvariantViolation, match="InvalidDirectionResponse.key"):
        InvalidDirectionResponse(DirectionResponseReason.KEY_SET_MISMATCH, "key")


def test_a004_empty_job_is_no_translate_without_call() -> None:
    client = FakeClient(AssertionError("must not call"))
    result = select_direction(client, ())
    assert result == DirectionSelectionResult(DirectionSelectionState.NO_TRANSLATE, None, (), None)
    assert result.complete_pairs == result.selected_pairs == result.unresolved_pairs == ()
    assert client.calls == []


@pytest.mark.parametrize(
    ("locale", "direction"),
    [(Locale.RU, Direction.RU_TO_EN), (Locale.EN, Direction.EN_TO_RU)],
)
def test_a005_a006_single_locale_is_deterministic_and_zero_call(
    locale: Locale, direction: Direction
) -> None:
    a = inventory("z.md", changed=(locale,), ru_content=b"same", en_content=b"same")
    b = inventory("a.md", changed=(locale,), state=PairFileState.RU_ONLY, ru_content=b"")
    client = FakeClient(AssertionError("must not call"))
    result = select_direction(client, (a, b))
    expected_verdict = (
        DirectionPairVerdict.RU_TO_EN
        if direction is Direction.RU_TO_EN
        else DirectionPairVerdict.EN_TO_RU
    )
    assert result.state is DirectionSelectionState.SELECTED
    assert result.direction is direction
    assert tuple(pair.key.relative_path.value for pair in result.selected_pairs) == ("a.md", "z.md")
    assert {decision.verdict for decision in result.decisions} == {expected_verdict}
    assert client.calls == []


@pytest.mark.parametrize("state", tuple(PairFileState))
@pytest.mark.parametrize("kind", tuple(ChangedFileKind))
def test_a007_state_and_change_kind_do_not_change_single_locale_direction(
    state: PairFileState, kind: ChangedFileKind
) -> None:
    base = inventory("any.md", state=state)
    old = base.ru.path
    if kind is ChangedFileKind.ADDED:
        change = ChangedMarkdownFile(ROOTS, kind, Locale.RU, base.key, None, old, None, None)
    elif kind is ChangedFileKind.DELETED:
        change = ChangedMarkdownFile(ROOTS, kind, Locale.RU, base.key, old, None, None, None)
    elif kind is ChangedFileKind.RENAMED:
        previous = PairKey(RepoPath("old.md"))
        change = ChangedMarkdownFile(
            ROOTS,
            kind,
            Locale.RU,
            base.key,
            RepoPath("ydb/docs/ru/old.md"),
            old,
            previous,
            __import__("ydbdoc_review_ng.locales", fromlist=["RenameContentState"]).RenameContentState.CHANGED,
        )
    else:
        change = _change(ROOTS, Locale.RU, base.key)
    pair = LocalePairInventory(ROOTS, base.key, base.ru, base.en, (change,), state)
    result = select_direction(FakeClient(AssertionError("must not call")), (pair,))
    assert result.direction is Direction.RU_TO_EN


def test_a008_a009_mixed_job_one_call_with_exact_projection() -> None:
    a = inventory(
        "z.md", changed=(Locale.RU, Locale.EN), ru_content=CANARY_BYTES, en_content=b""
    )
    b = inventory("a.md", changed=(Locale.RU,), state=PairFileState.RU_ONLY, ru_content=b"")
    c = inventory("m.md", changed=(Locale.EN,), state=PairFileState.EN_ONLY, en_content=b"EN")
    response = model_response(
        (a, DirectionPairVerdict.RU_TO_EN),
        (b, DirectionPairVerdict.RU_TO_EN),
        (c, DirectionPairVerdict.RU_TO_EN),
    )
    client = FakeClient(response, fail_on_second=True)
    result = select_direction(client, (a, c, b))
    assert result.direction is Direction.RU_TO_EN
    assert len(client.calls) == 1
    request = client.calls[0]
    assert request.role is ModelRole.DIRECTION
    assert request.operator_context is None
    assert tuple(item.key.relative_path.value for item in request.pairs) == (
        "a.md", "m.md", "z.md"
    )
    originals = {pair.key: pair for pair in (a, b, c)}
    for projected in request.pairs:
        original = originals[projected.key]
        assert projected.snapshot == original.ru.snapshot == original.en.snapshot
        assert projected.ru_path == original.ru.path
        assert projected.ru_content is original.ru.content
        assert projected.en_path == original.en.path
        assert projected.en_content is original.en.content


def test_a010_all_model_confirmed_complete_pairs_are_no_translate() -> None:
    pairs = tuple(
        inventory(name, changed=(Locale.RU, Locale.EN)) for name in ("a.md", "b.md")
    )
    client = FakeClient(
        model_response(*( (pair, DirectionPairVerdict.COMPLETE_PAIR) for pair in pairs ))
    )
    result = select_direction(client, pairs)
    assert result.state is DirectionSelectionState.NO_TRANSLATE
    assert result.direction is None
    assert result.complete_pairs == pairs
    assert result.selected_pairs == result.unresolved_pairs == ()
    assert result.diagnostic is None
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("verdict", "direction"),
    [
        (DirectionPairVerdict.RU_TO_EN, Direction.RU_TO_EN),
        (DirectionPairVerdict.EN_TO_RU, Direction.EN_TO_RU),
    ],
)
def test_a011_a012_mixed_agreed_direction_excludes_complete_pairs(
    verdict: DirectionPairVerdict, direction: Direction
) -> None:
    complete = inventory("a.md", changed=(Locale.RU, Locale.EN))
    selected = inventory("b.md", changed=(Locale.RU, Locale.EN))
    client = FakeClient(
        model_response(
            (complete, DirectionPairVerdict.COMPLETE_PAIR),
            (selected, verdict),
        )
    )
    result = select_direction(client, (selected, complete))
    assert result.state is DirectionSelectionState.SELECTED
    assert result.direction is direction
    assert result.complete_pairs == (complete,)
    assert result.selected_pairs == (selected,)
    assert result.unresolved_pairs == ()


def test_a013_ambiguous_job_has_exact_yellow_diagnostic() -> None:
    pair = inventory("a.md", changed=(Locale.RU, Locale.EN))
    result = select_direction(
        FakeClient(model_response((pair, DirectionPairVerdict.UNDETERMINED))), (pair,)
    )
    assert result.state is DirectionSelectionState.DIRECTION_UNDETERMINED
    assert result.direction is None
    assert result.selected_pairs == ()
    assert result.unresolved_pairs == (pair,)
    assert result.diagnostic == Diagnostic(
        Severity.YELLOW,
        "direction_undetermined",
        "Автоматический перевод не запущен: не удалось определить направление перевода",
        "Уточните исходные изменения и запустите новый `doc_translate`. "
        "Для проверки исправленной translation branch используйте `doc_verify`.",
    )


def test_a014_conflicts_normalize_all_non_complete_and_are_permutation_stable() -> None:
    complete = inventory("a.md", changed=(Locale.RU, Locale.EN))
    ru = inventory("b.md", changed=(Locale.RU, Locale.EN))
    en = inventory("c.md", changed=(Locale.RU, Locale.EN))
    response = model_response(
        (complete, DirectionPairVerdict.COMPLETE_PAIR),
        (ru, DirectionPairVerdict.RU_TO_EN),
        (en, DirectionPairVerdict.EN_TO_RU),
    )
    first_client = FakeClient(response)
    second_client = FakeClient(response)
    first = select_direction(first_client, (en, complete, ru))
    second = select_direction(second_client, (ru, en, complete))
    assert first == second
    assert first_client.calls == second_client.calls
    assert first.complete_pairs == (complete,)
    assert first.unresolved_pairs == (ru, en)
    assert {decision.verdict for decision in first.decisions[1:]} == {
        DirectionPairVerdict.UNDETERMINED
    }


def test_a015_directional_decision_survives_ambiguous_neighbor() -> None:
    retained = inventory("a.md", changed=(Locale.RU, Locale.EN))
    unresolved = inventory("b.md", changed=(Locale.RU, Locale.EN))
    result = select_direction(
        FakeClient(
            model_response(
                (retained, DirectionPairVerdict.RU_TO_EN),
                (unresolved, DirectionPairVerdict.UNDETERMINED),
            )
        ),
        (retained, unresolved),
    )
    assert result.state is DirectionSelectionState.DIRECTION_UNDETERMINED
    assert result.selected_pairs == ()
    assert result.unresolved_pairs == (unresolved,)
    assert result.decisions[0].verdict is DirectionPairVerdict.RU_TO_EN


@pytest.mark.parametrize(
    ("returned", "reason", "expected_key"),
    [
        (object(), DirectionResponseReason.WRONG_RESPONSE_TYPE, None),
    ],
)
def test_a016_wrong_response_type_fails_closed(
    returned: object, reason: DirectionResponseReason, expected_key: PairKey | None
) -> None:
    pair = inventory("a.md", changed=(Locale.RU, Locale.EN))
    client = FakeClient(returned)
    with pytest.raises(InvalidDirectionResponse) as caught:
        select_direction(client, (pair,))
    assert caught.value.reason is reason
    assert caught.value.key == expected_key
    assert len(client.calls) == 1
    assert_safe_direction_error(caught.value)


def test_a016_response_subclass_fails_closed_without_partial_result() -> None:
    class StrictDirectionModelResponse(DirectionModelResponse):
        __slots__ = ()

    pair = inventory("a.md", changed=(Locale.RU, Locale.EN))
    response = StrictDirectionModelResponse(
        (DirectionModelDecision(pair.key, DirectionPairVerdict.RU_TO_EN),)
    )
    client = FakeClient(response)
    result: DirectionSelectionResult | None = None
    with pytest.raises(InvalidDirectionResponse) as caught:
        result = select_direction(client, (pair,))
    assert result is None
    assert caught.value.reason is DirectionResponseReason.WRONG_RESPONSE_TYPE
    assert caught.value.key is None
    assert caught.value.args == ("invalid_direction_response:wrong_response_type",)
    assert str(caught.value) == "invalid_direction_response:wrong_response_type"
    assert len(client.calls) == 1
    assert_safe_direction_error(caught.value)


def test_a016_key_set_mismatch_uses_smallest_symmetric_difference() -> None:
    a = inventory("a.md", changed=(Locale.RU, Locale.EN))
    b = inventory("b.md", changed=(Locale.RU, Locale.EN))
    c = inventory("c.md", changed=(Locale.RU, Locale.EN))
    response = model_response(
        (b, DirectionPairVerdict.RU_TO_EN),
        (c, DirectionPairVerdict.RU_TO_EN),
    )
    client = FakeClient(response)
    with pytest.raises(InvalidDirectionResponse) as caught:
        select_direction(client, (b, a))
    assert caught.value.reason is DirectionResponseReason.KEY_SET_MISMATCH
    assert caught.value.key == a.key
    assert len(client.calls) == 1
    assert_safe_direction_error(caught.value)


@pytest.mark.parametrize(
    "state",
    [PairFileState.RU_ONLY, PairFileState.EN_ONLY, PairFileState.BOTH_MISSING],
)
def test_a016_complete_verdict_requires_both_present(state: PairFileState) -> None:
    a = inventory("a.md", changed=(Locale.RU, Locale.EN), state=state)
    b = inventory("b.md", changed=(Locale.RU, Locale.EN), state=state)
    client = FakeClient(
        model_response(
            (a, DirectionPairVerdict.COMPLETE_PAIR),
            (b, DirectionPairVerdict.COMPLETE_PAIR),
        )
    )
    with pytest.raises(InvalidDirectionResponse) as caught:
        select_direction(client, (b, a))
    assert caught.value.reason is DirectionResponseReason.COMPLETE_PAIR_REQUIRES_BOTH_PRESENT
    assert caught.value.key == a.key
    assert len(client.calls) == 1


def test_a017_client_exception_propagates_by_identity_and_is_not_retried() -> None:
    pair = inventory("a.md", changed=(Locale.RU, Locale.EN))
    sentinel = RuntimeError("CLIENT-OWNED-CANARY")

    def fail(_: DirectionModelRequest) -> object:
        raise sentinel

    client = FakeClient(fail)
    with pytest.raises(RuntimeError) as caught:
        select_direction(client, (pair,))
    assert caught.value is sentinel
    assert str(caught.value) == "CLIENT-OWNED-CANARY"
    assert len(client.calls) == 1


def test_a022_input_validation_precedence_and_canonical_keys() -> None:
    duplicate_z = inventory("z.md")
    empty_a = inventory("a.md", changed=())
    mixed_b = inventory("b.md", roots=OTHER_ROOTS)
    snapshot_c = inventory("c.md", snapshot=OTHER_SNAPSHOT)
    client = FakeClient(AssertionError("must not call"))
    with pytest.raises(InvalidDirectionInput) as caught:
        select_direction(client, (snapshot_c, duplicate_z, empty_a, duplicate_z, mixed_b))
    assert (caught.value.reason, caught.value.key) == (
        DirectionInputReason.DUPLICATE_PAIR_KEY,
        duplicate_z.key,
    )
    assert client.calls == []

    for supplied in (
        (snapshot_c, empty_a, mixed_b),
        (mixed_b, snapshot_c, empty_a),
    ):
        with pytest.raises(InvalidDirectionInput) as caught:
            select_direction(client, supplied)
        assert (caught.value.reason, caught.value.key) == (
            DirectionInputReason.EMPTY_PAIR_CHANGES,
            empty_a.key,
        )

    roots_a = inventory("a.md", roots=ROOTS)
    roots_b = inventory("b.md", roots=OTHER_ROOTS)
    with pytest.raises(InvalidDirectionInput) as caught:
        select_direction(client, (roots_b, roots_a))
    assert caught.value.reason is DirectionInputReason.MIXED_ROOTS
    assert caught.value.key == roots_a.key

    snap_a = inventory("a.md", snapshot=SNAPSHOT)
    snap_b = inventory("b.md", snapshot=OTHER_SNAPSHOT)
    with pytest.raises(InvalidDirectionInput) as caught:
        select_direction(client, (snap_b, snap_a))
    assert caught.value.reason is DirectionInputReason.MIXED_SNAPSHOTS
    assert caught.value.key == snap_b.key


@pytest.mark.parametrize("inventories", [[], (object(),)])
def test_a022_wrong_inventory_container_or_element_is_zero_call(
    inventories: object,
) -> None:
    client = FakeClient(AssertionError("must not call"))
    with pytest.raises(InvariantViolation, match="select_direction.inventories"):
        select_direction(client, cast(tuple[LocalePairInventory, ...], inventories))
    assert client.calls == []


def test_a022_valid_permutations_preserve_inputs_and_equal_ledgers() -> None:
    a = inventory("a.md", changed=(Locale.RU, Locale.EN), ru_content=CANARY_BYTES)
    b = inventory("b.md", changed=(Locale.RU, Locale.EN), en_content=b"")
    response = model_response(
        (a, DirectionPairVerdict.RU_TO_EN),
        (b, DirectionPairVerdict.RU_TO_EN),
    )
    first_client = FakeClient(response)
    second_client = FakeClient(response)
    first = select_direction(first_client, (b, a))
    second = select_direction(second_client, (a, b))
    assert first == second
    assert first_client.calls == second_client.calls
    assert a.ru.content is CANARY_BYTES
    assert b.en.content == b""


def test_a023_no_serialization_transport_or_future_policy_leak() -> None:
    pair = inventory("a.md")
    values: tuple[object, ...] = (
        Direction.RU_TO_EN,
        DirectionModelPair(pair.key, SNAPSHOT, pair.ru.path, b"", pair.en.path, b""),
        DirectionModelRequest(
            ModelRole.DIRECTION,
            (DirectionModelPair(pair.key, SNAPSHOT, pair.ru.path, b"", pair.en.path, b""),),
            None,
        ),
        DirectionModelResponse((DirectionModelDecision(pair.key, DirectionPairVerdict.RU_TO_EN),)),
        DirectionSelectionResult(
            DirectionSelectionState.SELECTED,
            Direction.RU_TO_EN,
            (DirectionPairDecision(pair, DirectionPairVerdict.RU_TO_EN),),
            None,
        ),
    )
    for value in values:
        with pytest.raises(UnknownDomainType):
            to_wire(cast(object, value))

    source_path = Path(__file__).parents[2] / "src/ydbdoc_review_ng/direction.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imported_modules <= {
        "__future__",
        "dataclasses",
        "enum",
        "ydbdoc_review_ng.domain",
        "ydbdoc_review_ng.errors",
        "ydbdoc_review_ng.locales",
        "ydbdoc_review_ng.ports",
    }
    forbidden = {
        "SnapshotReader",
        "os",
        "subprocess",
        "requests",
        "github",
        "ydb",
        "asdict",
        "TRANSLATE",
        "CRITIC",
        "REPAIR",
        "FINAL_CRITIC",
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert forbidden.isdisjoint(names | attributes)


def test_a003_enum_subclass_and_request_pair_subclass_are_rejected() -> None:
    class PairKeySubclass(PairKey):
        pass

    pair = inventory("a.md")
    with pytest.raises(InvariantViolation, match="DirectionModelDecision.key"):
        DirectionModelDecision(
            PairKeySubclass(pair.key.relative_path), DirectionPairVerdict.RU_TO_EN
        )


def test_a003_all_public_tuple_elements_reject_strict_subclasses() -> None:
    class DirectionModelPairSubclass(DirectionModelPair):
        __slots__ = ()

    class DirectionModelDecisionSubclass(DirectionModelDecision):
        __slots__ = ()

    class DirectionPairDecisionSubclass(DirectionPairDecision):
        __slots__ = ()

    pair = inventory("a.md")
    cases: tuple[tuple[Callable[[], object], str], ...] = (
        (
            lambda: DirectionModelRequest(
                ModelRole.DIRECTION,
                (
                    DirectionModelPairSubclass(
                        pair.key,
                        SNAPSHOT,
                        pair.ru.path,
                        pair.ru.content,
                        pair.en.path,
                        pair.en.content,
                    ),
                ),
                None,
            ),
            "DirectionModelRequest.pairs: expected exact DirectionModelPair",
        ),
        (
            lambda: DirectionModelResponse(
                (
                    DirectionModelDecisionSubclass(
                        pair.key, DirectionPairVerdict.RU_TO_EN
                    ),
                )
            ),
            "DirectionModelResponse.decisions: expected exact DirectionModelDecision",
        ),
        (
            lambda: DirectionSelectionResult(
                DirectionSelectionState.SELECTED,
                Direction.RU_TO_EN,
                (
                    DirectionPairDecisionSubclass(
                        pair, DirectionPairVerdict.RU_TO_EN
                    ),
                ),
                None,
            ),
            "DirectionSelectionResult.decisions: expected exact DirectionPairDecision",
        ),
    )

    for construct, expected in cases:
        with pytest.raises(InvariantViolation) as caught:
            construct()
        assert caught.value.args == (expected,)
        assert str(caught.value) == expected
