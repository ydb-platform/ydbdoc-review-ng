"""Continuation state v3: no file contents in checkpoint state."""

from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng.continuation import (
    STATE_VERSION,
    ContinuationStage,
    ContinuationState,
    ContinuationStateError,
    decode_state,
    encode_state,
)
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import ContentHash, GitSha, RepoPath

PENDING = RepoPath("en/b.md")
REVIEW = RepoPath("en/a.md")


def test_state_version_is_three() -> None:
    assert STATE_VERSION == 3


def test_v2_state_with_accepted_documents_is_rejected() -> None:
    v2 = {
        "state_version": 2,
        "stage": "translation",
        "direction": "ru_to_en",
        "scope_sha256": "b" * 64,
        "accepted_documents": {"en/a.md": "# Translated\n"},
        "pending_paths": ["en/b.md"],
        "review_paths": [],
        "candidate_sha256": None,
    }
    with pytest.raises(ContinuationStateError):
        decode_state(json.dumps(v2))


def test_v3_translation_round_trip_without_file_contents() -> None:
    state = ContinuationState(
        STATE_VERSION,
        ContinuationStage.TRANSLATION,
        Direction.RU_TO_EN,
        ContentHash("b" * 64),
        GitSha("c" * 40),
        (PENDING,),
        (),
    )
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
    assert payload["target_sha"] == "c" * 40
    assert decode_state(encoded) == state


def test_v3_direction_and_review_shapes() -> None:
    direction = ContinuationState(
        STATE_VERSION, ContinuationStage.DIRECTION, None, None, None, (), ()
    )
    review = ContinuationState(
        STATE_VERSION,
        ContinuationStage.REVIEW,
        Direction.EN_TO_RU,
        ContentHash("d" * 64),
        GitSha("e" * 40),
        (),
        (REVIEW,),
    )
    assert decode_state(encode_state(direction)) == direction
    assert decode_state(encode_state(review)) == review


def test_v3_review_requires_target_sha() -> None:
    with pytest.raises(ContinuationStateError):
        ContinuationState(
            STATE_VERSION,
            ContinuationStage.REVIEW,
            Direction.EN_TO_RU,
            ContentHash("d" * 64),
            None,
            (),
            (REVIEW,),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pending_paths": (), "review_paths": ()},
        {"review_paths": (REVIEW,)},
        {"direction": None},
        {"scope_sha256": None},
    ],
)
def test_v3_translation_rejects_invalid_combinations(kwargs) -> None:
    values = {
        "state_version": STATE_VERSION,
        "stage": ContinuationStage.TRANSLATION,
        "direction": Direction.RU_TO_EN,
        "scope_sha256": ContentHash("b" * 64),
        "target_sha": None,
        "pending_paths": (PENDING,),
        "review_paths": (),
    }
    values.update(kwargs)
    with pytest.raises(ContinuationStateError):
        ContinuationState(**values)
