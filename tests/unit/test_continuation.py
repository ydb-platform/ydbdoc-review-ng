"""Continuation state codec and stage invariants for STATE_VERSION=3."""

from __future__ import annotations

import dataclasses
import json
from hashlib import sha256

import pytest

from ydbdoc_review_ng.continuation import (
    STATE_VERSION,
    AcceptedDocument,
    ContinuationStage,
    ContinuationState,
    ContinuationStateError,
    RestoredPlan,
    candidate_sha256,
    decode_state,
    encode_state,
    validate_restored_documents,
)
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import (
    ContentHash,
    FilePair,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import LocaleRoots, PairKey
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.scope import (
    FileOperation,
    InitialPairDisposition,
    InitialPairOutcome,
    ScopeEntry,
    ScopeManifest,
    ScopeOrigin,
)

SOURCE = b'# Heading\n\nSee https://example.test/a.\n\n```python\n# comment\nprint("x")\n```\n'
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
SOURCE_PATH = RepoPath("ru/a.md")
TARGET_PATH = RepoPath("en/a.md")
PENDING_PATH = RepoPath("en/b.md")
TARGET_SHA = GitSha("c" * 40)


def test_v1_and_v2_states_are_rejected() -> None:
    for version, extra in (
        (
            1,
            {
                "accepted_maps": {},
                "pending_paths": ["en/b.md"],
                "review_paths": [],
                "candidate_sha256": None,
            },
        ),
        (
            2,
            {
                "accepted_documents": {"en/a.md": "# Complete translated Markdown\n"},
                "pending_paths": ["en/b.md"],
                "review_paths": [],
                "candidate_sha256": None,
            },
        ),
    ):
        payload = {
            "state_version": version,
            "stage": "translation",
            "direction": "ru_to_en",
            "scope_sha256": "b" * 64,
            **extra,
        }
        with pytest.raises(ContinuationStateError):
            decode_state(json.dumps(payload))


def pending_plan() -> RestoredPlan:
    plan = build_markdown_plan(SNAPSHOT, RepoPath("ru/b.md"), SOURCE)
    return RestoredPlan(PENDING_PATH, SOURCE, plan)


def restored_plan() -> RestoredPlan:
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, SOURCE)
    return RestoredPlan(TARGET_PATH, SOURCE, plan)


def translation_state() -> ContinuationState:
    return ContinuationState(
        STATE_VERSION,
        ContinuationStage.TRANSLATION,
        Direction.RU_TO_EN,
        ContentHash("b" * 64),
        None,
        (PENDING_PATH,),
        (),
    )


def test_state_round_trip_without_file_contents() -> None:
    state = translation_state()
    encoded = encode_state(state)
    payload = json.loads(encoded)
    assert set(payload) == {
        "state_version",
        "stage",
        "direction",
        "scope_sha256",
        "target_sha",
        "pending_paths",
        "review_paths",
    }
    assert "accepted_documents" not in payload
    assert "candidate_sha256" not in payload
    assert decode_state(encoded) == state
    validate_restored_documents(state, (restored_plan(), pending_plan()))


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        json.dumps({**json.loads(encode_state(translation_state())), "unknown": 1}),
        encode_state(translation_state()).replace('"state_version":3', '"state_version":true'),
        encode_state(translation_state()).replace(
            '"pending_paths":["en/b.md"]', '"pending_paths":{}'
        ),
        json.dumps(
            {**json.loads(encode_state(translation_state())), "accepted_documents": {}},
            separators=(",", ":"),
            sort_keys=True,
        ),
    ],
)
def test_decode_rejects_missing_unknown_and_non_exact_primitive_or_container_types(
    raw: str,
) -> None:
    json.loads(raw)
    with pytest.raises(ContinuationStateError):
        decode_state(raw)


def test_decode_rejects_duplicate_json_keys() -> None:
    encoded = encode_state(translation_state())
    duplicated = encoded.replace('"state_version":3', '"state_version":3,"state_version":3')
    with pytest.raises(ContinuationStateError):
        decode_state(duplicated)


@pytest.mark.parametrize(
    "changes",
    [
        {"direction": Direction.RU_TO_EN},
        {"scope_sha256": ContentHash("b" * 64)},
        {"target_sha": TARGET_SHA},
        {"pending_paths": (TARGET_PATH,)},
        {"review_paths": (TARGET_PATH,)},
    ],
)
def test_direction_stage_rejects_incompatible_fields(changes: dict[str, object]) -> None:
    state = ContinuationState(
        STATE_VERSION,
        ContinuationStage.DIRECTION,
        None,
        None,
        None,
        (),
        (),
    )
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, **changes)


def test_translation_and_review_stages_reject_incompatible_fields() -> None:
    state = translation_state()
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, review_paths=(TARGET_PATH,))
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, pending_paths=(PENDING_PATH, PENDING_PATH))
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, pending_paths=())

    review = ContinuationState(
        STATE_VERSION,
        ContinuationStage.REVIEW,
        Direction.RU_TO_EN,
        ContentHash("b" * 64),
        TARGET_SHA,
        (),
        (TARGET_PATH,),
    )
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(review, pending_paths=(PENDING_PATH,))
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(review, target_sha=None)
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(review, review_paths=())


def test_candidate_sha256_helper_still_hashes_exact_bytes() -> None:
    assert candidate_sha256(b"candidate") == ContentHash(sha256(b"candidate").hexdigest())
    with pytest.raises(TypeError):
        candidate_sha256(bytearray(b"candidate"))  # type: ignore[arg-type]


def test_validate_restored_documents_binds_pending_and_review_paths() -> None:
    state = translation_state()
    validate_restored_documents(state, (restored_plan(), pending_plan()))
    with pytest.raises(ContinuationStateError):
        validate_restored_documents(state, (restored_plan(),))

    review = ContinuationState(
        STATE_VERSION,
        ContinuationStage.REVIEW,
        Direction.RU_TO_EN,
        ContentHash("b" * 64),
        TARGET_SHA,
        (),
        (TARGET_PATH,),
    )
    validate_restored_documents(review, (restored_plan(),))
