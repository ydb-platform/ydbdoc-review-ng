"""Bridge types retained for scope freeze after inventory classifier owns direction."""

from __future__ import annotations

import pytest

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
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.locales import (
    ChangedFileKind,
    ChangedMarkdownFile,
    LocalePairInventory,
    LocaleRoots,
    PairFileState,
    PairKey,
    SnapshotLocaleFile,
)

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
ROOTS = LocaleRoots(RepoPath("ydb/docs/ru"), RepoPath("ydb/docs/en"))


def _pair(key: str, *, both: bool = True) -> LocalePairInventory:
    pair_key = PairKey(RepoPath(key))
    ru_path = RepoPath(f"ydb/docs/ru/{key}")
    en_path = RepoPath(f"ydb/docs/en/{key}")
    change = ChangedMarkdownFile(
        ROOTS,
        ChangedFileKind.MODIFIED,
        Locale.RU,
        pair_key,
        ru_path,
        ru_path,
        None,
        None,
    )
    return LocalePairInventory(
        ROOTS,
        pair_key,
        SnapshotLocaleFile(ROOTS, Locale.RU, pair_key, ru_path, SNAPSHOT, b"# ru\n"),
        SnapshotLocaleFile(
            ROOTS,
            Locale.EN,
            pair_key,
            en_path,
            SNAPSHOT,
            b"# en\n" if both else None,
        ),
        (change,),
        PairFileState.BOTH_PRESENT if both else PairFileState.RU_ONLY,
    )


def test_public_api_excludes_dead_per_pair_model_path() -> None:
    import ydbdoc_review_ng.direction as direction

    assert not hasattr(direction, "select_direction")
    assert not hasattr(direction, "DirectionModelRequest")
    assert not hasattr(direction, "DirectionModelResponse")
    assert "select_direction" not in direction.__all__


def test_selected_result_requires_matching_non_complete_verdicts() -> None:
    pair = _pair("a.md")
    result = DirectionSelectionResult(
        DirectionSelectionState.SELECTED,
        Direction.RU_TO_EN,
        (DirectionPairDecision(pair, DirectionPairVerdict.RU_TO_EN),),
        None,
    )
    assert result.selected_pairs == (pair,)
    assert result.complete_pairs == ()
    assert result.unresolved_pairs == ()


def test_no_translate_allows_only_complete_pairs() -> None:
    pair = _pair("a.md")
    result = DirectionSelectionResult(
        DirectionSelectionState.NO_TRANSLATE,
        None,
        (DirectionPairDecision(pair, DirectionPairVerdict.COMPLETE_PAIR),),
        None,
    )
    assert result.complete_pairs == (pair,)
    assert result.selected_pairs == ()


def test_undetermined_uses_canonical_diagnostic() -> None:
    pair = _pair("a.md")
    diagnostic = Diagnostic(
        Severity.YELLOW,
        "direction_undetermined",
        DIRECTION_UNDETERMINED_WARNING,
        DIRECTION_UNDETERMINED_ACTION,
    )
    result = DirectionSelectionResult(
        DirectionSelectionState.DIRECTION_UNDETERMINED,
        None,
        (DirectionPairDecision(pair, DirectionPairVerdict.UNDETERMINED),),
        diagnostic,
    )
    assert result.unresolved_pairs == (pair,)
    assert "doc_verify" in DIRECTION_UNDETERMINED_ACTION


def test_complete_verdict_requires_both_present() -> None:
    pair = _pair("a.md", both=False)
    with pytest.raises(InvariantViolation, match="complete verdicts"):
        DirectionSelectionResult(
            DirectionSelectionState.NO_TRANSLATE,
            None,
            (DirectionPairDecision(pair, DirectionPairVerdict.COMPLETE_PAIR),),
            None,
        )
