from __future__ import annotations

from pathlib import Path

from ydbdoc_review_ng.direction import (
    DirectionModelDecision,
    DirectionModelRequest,
    DirectionModelResponse,
    DirectionPairVerdict,
    DirectionSelectionState,
    select_direction,
)
from ydbdoc_review_ng.domain import GitSha, Locale, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.locales import (
    ChangedFileKind,
    ChangedMarkdownFile,
    LocalePairInventory,
    LocaleRoots,
    PairFileState,
    PairKey,
    SnapshotLocaleFile,
)

FIXTURE_PR = "ydb-platform/ydb#51079"
FIXTURE_SHA = "5aab6d0e65926540eff571d03a018a492face7e1"
FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures/pairs"


class DirectionFake:
    def __init__(self) -> None:
        self.calls: list[DirectionModelRequest] = []

    def invoke(self, request: DirectionModelRequest, /) -> DirectionModelResponse:
        self.calls.append(request)
        verdicts = (
            DirectionPairVerdict.COMPLETE_PAIR,
            DirectionPairVerdict.UNDETERMINED,
        )
        return DirectionModelResponse(
            tuple(
                DirectionModelDecision(pair.key, verdict)
                for pair, verdict in zip(request.pairs, verdicts, strict=True)
            )
        )


def _inventory(
    roots: LocaleRoots,
    snapshot: SnapshotRef,
    relative_path: str,
    ru_content: bytes,
    en_content: bytes,
) -> LocalePairInventory:
    key = PairKey(RepoPath(relative_path))
    ru_path = RepoPath(f"{roots.ru.value}/{relative_path}")
    en_path = RepoPath(f"{roots.en.value}/{relative_path}")
    changes = (
        ChangedMarkdownFile(
            roots,
            ChangedFileKind.MODIFIED,
            Locale.RU,
            key,
            ru_path,
            ru_path,
            None,
            None,
        ),
        ChangedMarkdownFile(
            roots,
            ChangedFileKind.MODIFIED,
            Locale.EN,
            key,
            en_path,
            en_path,
            None,
            None,
        ),
    )
    return LocalePairInventory(
        roots,
        key,
        SnapshotLocaleFile(roots, Locale.RU, key, ru_path, snapshot, ru_content),
        SnapshotLocaleFile(roots, Locale.EN, key, en_path, snapshot, en_content),
        changes,
        PairFileState.BOTH_PRESENT,
    )


def test_a024_offline_t004_to_t005_selection_stops_on_ambiguous_direction() -> None:
    provenance = (FIXTURE_DIRECTORY / "PROVENANCE.md").read_text(encoding="utf-8")
    assert FIXTURE_PR in provenance
    assert FIXTURE_SHA in provenance
    pr_51079_ru = (FIXTURE_DIRECTORY / "authentication.slice").read_bytes()
    pr_51079_en = (FIXTURE_DIRECTORY / "auth_config.slice").read_bytes()
    assert pr_51079_ru
    assert pr_51079_en
    roots = LocaleRoots(RepoPath("ydb/docs/ru"), RepoPath("ydb/docs/en"))
    snapshot = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha(FIXTURE_SHA))
    generated_ru = b"Generated RU counterpart for direction-flow coverage.\n"
    generated_en = b"Generated incomplete EN counterpart.\n"
    inventories = (
        _inventory(
            roots,
            snapshot,
            "security/auth.md",
            pr_51079_ru,
            pr_51079_en,
        ),
        _inventory(
            roots,
            snapshot,
            "security/generated-counterpart.md",
            generated_ru,
            generated_en,
        ),
    )
    client = DirectionFake()

    first = select_direction(client, inventories)
    assert first.state is DirectionSelectionState.DIRECTION_UNDETERMINED
    assert tuple(pair.key.relative_path.value for pair in first.complete_pairs) == (
        "security/auth.md",
    )
    assert tuple(pair.key.relative_path.value for pair in first.unresolved_pairs) == (
        "security/generated-counterpart.md",
    )

    assert first.selected_pairs == ()
    assert len(client.calls) == 1
    assert client.calls[0].pairs[1].ru_content is generated_ru
    assert client.calls[0].pairs[1].en_content is generated_en
