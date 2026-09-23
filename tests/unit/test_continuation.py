from __future__ import annotations

import dataclasses
import json
from hashlib import sha256

import pytest

from ydbdoc_review_ng import continuation
from ydbdoc_review_ng.continuation import (
    STATE_VERSION,
    AcceptedDocument,
    AcceptedMap,
    ContinuationStage,
    ContinuationState,
    ContinuationStateError,
    RestoredPlan,
    candidate_sha256,
    decode_state,
    encode_state,
    scope_sha256,
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
from ydbdoc_review_ng.translation.assembly import assemble_candidate
from ydbdoc_review_ng.translation.contract import build_translation_request

SOURCE = b'# Heading\n\nSee https://example.test/a.\n\n```python\n# comment\nprint("x")\n```\n'
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
SOURCE_PATH = RepoPath("ru/a.md")
TARGET_PATH = RepoPath("en/a.md")
PENDING_PATH = RepoPath("en/b.md")


def test_v1_state_is_rejected_and_v2_round_trip_persists_complete_documents() -> None:
    v1 = {
        "state_version": 1,
        "stage": "translation",
        "direction": "ru_to_en",
        "scope_sha256": "b" * 64,
        "accepted_maps": {},
        "pending_paths": ["en/b.md"],
        "review_paths": [],
        "candidate_sha256": None,
    }
    with pytest.raises(ContinuationStateError):
        decode_state(json.dumps(v1))

    document_type = continuation.AcceptedDocument
    state = ContinuationState(
        2,
        ContinuationStage.TRANSLATION,
        Direction.RU_TO_EN,
        ContentHash("b" * 64),
        (document_type(TARGET_PATH, "# Complete translated Markdown\n"),),
        (PENDING_PATH,),
        (),
        None,
    )

    encoded = encode_state(state)
    assert json.loads(encoded)["accepted_documents"] == {
        "en/a.md": "# Complete translated Markdown\n"
    }
    assert decode_state(encoded) == state


def restored_plan() -> tuple[RestoredPlan, dict[str, str]]:
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, SOURCE)
    request = build_translation_request(SOURCE, plan)
    translations = {
        request.fields[0].field_id: "Title",
        request.fields[1].field_id: "Read [[URL_0001]].",
        request.fields[2].field_id: "translated comment",
    }
    return RestoredPlan(TARGET_PATH, SOURCE, plan), translations


def pending_plan() -> RestoredPlan:
    plan = build_markdown_plan(SNAPSHOT, RepoPath("ru/b.md"), SOURCE)
    return RestoredPlan(PENDING_PATH, SOURCE, plan)


def translation_state() -> ContinuationState:
    restored, translations = restored_plan()
    request = build_translation_request(restored.source, restored.plan)
    translated = assemble_candidate(
        restored.source, restored.plan, request, translations
    ).decode("utf-8")
    return ContinuationState(
        STATE_VERSION,
        ContinuationStage.TRANSLATION,
        Direction.RU_TO_EN,
        ContentHash("b" * 64),
        (AcceptedDocument(restored.target_path, translated),),
        (PENDING_PATH,),
        (),
        None,
    )


def manifest(
    *,
    direction: Direction = Direction.RU_TO_EN,
    operation: FileOperation = FileOperation.TRANSLATE,
    source: bytes | None = SOURCE,
    relative_path: str = "a.md",
) -> ScopeManifest:
    source_locale, target_locale = (
        (Locale.RU, Locale.EN) if direction is Direction.RU_TO_EN else (Locale.EN, Locale.RU)
    )
    source_path, target_path = (
        (RepoPath(f"ru/{relative_path}"), RepoPath(f"en/{relative_path}"))
        if direction is Direction.RU_TO_EN
        else (RepoPath(f"en/{relative_path}"), RepoPath(f"ru/{relative_path}"))
    )
    key = PairKey(RepoPath(relative_path))
    entry = ScopeEntry(
        FilePair(source_locale, target_locale, source_path, target_path),
        source,
        b"old target" if operation is FileOperation.DELETE_TARGET else None,
        ScopeOrigin.INITIAL,
        operation,
        (key,),
        RepoPath(f"{target_locale.value}/old/{relative_path}")
        if operation is FileOperation.RENAME_TARGET_AND_TRANSLATE
        else None,
        b"old target" if operation is FileOperation.RENAME_TARGET_AND_TRANSLATE else None,
    )
    return ScopeManifest(
        direction,
        SNAPSHOT,
        LocaleRoots(RepoPath("ru"), RepoPath("en")),
        (InitialPairOutcome(key, InitialPairDisposition.SELECTED, entry),),
        (entry,),
        0,
        len(source.decode("utf-8")) if source is not None else 0,
    )


def test_state_round_trip_restores_real_plan_and_source_owned_fragments() -> None:
    restored, translations = restored_plan()
    state = translation_state()

    encoded = encode_state(state)
    payload = json.loads(encoded)
    assert set(payload) == {
        "state_version",
        "stage",
        "direction",
        "scope_sha256",
        "accepted_documents",
        "pending_paths",
        "review_paths",
        "candidate_sha256",
    }
    assert decode_state(encoded) == state
    validate_restored_documents(state, (restored, pending_plan()))

    request = build_translation_request(restored.source, restored.plan)
    candidate = assemble_candidate(restored.source, restored.plan, request, translations)
    assert b"https://example.test/a" in candidate
    assert b"# translated comment" in candidate


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        json.dumps({**json.loads(encode_state(translation_state())), "unknown": 1}),
        encode_state(translation_state()).replace('"state_version":2', '"state_version":true'),
        encode_state(translation_state()).replace(
            '"pending_paths":["en/b.md"]', '"pending_paths":{}'
        ),
        json.dumps(
            {**json.loads(encode_state(translation_state())), "accepted_documents": []},
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


def duplicate_key_payloads() -> tuple[str, str]:
    encoded = encode_state(translation_state())
    payload = json.loads(encoded)
    translated = json.dumps(payload["accepted_documents"][TARGET_PATH.value])
    accepted_document = (
        f'"accepted_documents":{{"{TARGET_PATH.value}":{translated}}}'
    )
    duplicate_document = (
        f'"accepted_documents":{{"{TARGET_PATH.value}":{translated},'
        f'"{TARGET_PATH.value}":{translated}}}'
    )
    return (
        encoded.replace('"state_version":2', '"state_version":2,"state_version":2'),
        encoded.replace(accepted_document, duplicate_document),
    )


@pytest.mark.parametrize("raw", duplicate_key_payloads())
def test_decode_rejects_duplicate_json_keys_at_every_object_level(raw: str) -> None:
    deduplicated = json.dumps(json.loads(raw), separators=(",", ":"), sort_keys=True)
    assert decode_state(deduplicated) == translation_state()
    with pytest.raises(ContinuationStateError):
        decode_state(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"direction": Direction.RU_TO_EN},
        {"scope_sha256": ContentHash("b" * 64)},
        {"accepted_documents": (AcceptedDocument(TARGET_PATH, "# target\n"),)},
        {"pending_paths": (TARGET_PATH,)},
        {"review_paths": (TARGET_PATH,)},
        {"candidate_sha256": ContentHash("c" * 64)},
    ],
)
def test_direction_stage_rejects_incompatible_fields(changes: dict[str, object]) -> None:
    state = ContinuationState(
        STATE_VERSION,
        ContinuationStage.DIRECTION,
        None,
        None,
        (),
        (),
        (),
        None,
    )
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, **changes)


def test_translation_and_review_stages_reject_incompatible_fields_and_duplicate_paths() -> None:
    state = translation_state()
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, review_paths=(TARGET_PATH,))
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, candidate_sha256=ContentHash("c" * 64))
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, pending_paths=(PENDING_PATH, PENDING_PATH))
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, pending_paths=(TARGET_PATH,))
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(state, pending_paths=())

    review = dataclasses.replace(
        state,
        stage=ContinuationStage.REVIEW,
        pending_paths=(),
        review_paths=(TARGET_PATH,),
        candidate_sha256=ContentHash("c" * 64),
    )
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(review, pending_paths=(RepoPath("en/pending.md"),))
    with pytest.raises(ContinuationStateError):
        dataclasses.replace(review, review_paths=())


def test_scope_hash_covers_direction_operation_paths_and_authoritative_source_hash() -> None:
    base = manifest()
    baseline = scope_sha256(base)
    assert scope_sha256(base) == baseline
    assert scope_sha256(manifest(direction=Direction.EN_TO_RU)) != baseline
    assert scope_sha256(manifest(operation=FileOperation.RENAME_TARGET_AND_TRANSLATE)) != baseline

    target_changed_entry = dataclasses.replace(
        base.entries[0],
        pair=dataclasses.replace(base.entries[0].pair, target_path=RepoPath("en/sub/a.md")),
    )
    target_changed = dataclasses.replace(
        base,
        entries=(target_changed_entry,),
        initial_outcomes=(
            dataclasses.replace(base.initial_outcomes[0], entry=target_changed_entry),
        ),
    )
    assert scope_sha256(target_changed) != baseline

    source_changed_entry = dataclasses.replace(
        base.entries[0],
        pair=dataclasses.replace(base.entries[0].pair, source_path=RepoPath("ru/sub/a.md")),
    )
    source_changed = dataclasses.replace(
        base,
        entries=(source_changed_entry,),
        initial_outcomes=(
            dataclasses.replace(base.initial_outcomes[0], entry=source_changed_entry),
        ),
    )
    assert scope_sha256(source_changed) != baseline

    changed_source = SOURCE.replace(b"Heading", b"Changed")
    assert scope_sha256(manifest(source=changed_source)) != baseline

    entry = {
        "operation": "translate",
        "rename_from_target_path": None,
        "source_path": "ru/a.md",
        "target_path": "en/a.md",
        "source_sha256": sha256(SOURCE).hexdigest(),
    }
    left = {"direction": "ru_to_en", "entries": [entry]}
    right = {"entries": [dict(reversed(tuple(entry.items())))], "direction": "ru_to_en"}
    assert (
        sha256(json.dumps(left, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        == sha256(json.dumps(right, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        == baseline.value
    )


def test_scope_hash_covers_nullable_rename_from_target_path_independently() -> None:
    renamed = manifest(operation=FileOperation.RENAME_TARGET_AND_TRANSLATE)
    renamed_entry = dataclasses.replace(
        renamed.entries[0], rename_from_target_path=RepoPath("en/another/a.md")
    )
    renamed_from_changed = dataclasses.replace(
        renamed,
        entries=(renamed_entry,),
        initial_outcomes=(dataclasses.replace(renamed.initial_outcomes[0], entry=renamed_entry),),
    )

    assert scope_sha256(renamed_from_changed) != scope_sha256(renamed)


def test_hash_helpers_return_exact_content_hashes() -> None:
    assert candidate_sha256(b"candidate") == ContentHash(sha256(b"candidate").hexdigest())
    with pytest.raises(TypeError):
        candidate_sha256(bytearray(b"candidate"))  # type: ignore[arg-type]


def test_restored_documents_require_known_paths_and_utf8_text() -> None:
    restored, _translations = restored_plan()
    state = translation_state()

    with pytest.raises(ContinuationStateError):
        validate_restored_documents(state, ())

    unknown_pending = dataclasses.replace(
        state,
        accepted_documents=(),
        pending_paths=(RepoPath("en/missing.md"),),
    )
    with pytest.raises(ContinuationStateError):
        validate_restored_documents(unknown_pending, (restored,))


def test_value_objects_are_frozen_and_require_exact_container_types() -> None:
    restored, translations = restored_plan()
    accepted = AcceptedMap(TARGET_PATH, tuple(translations.items()))
    with pytest.raises(dataclasses.FrozenInstanceError):
        accepted.target_path = RepoPath("en/b.md")  # type: ignore[misc]
    with pytest.raises(ContinuationStateError):
        AcceptedMap(TARGET_PATH, list(translations.items()))  # type: ignore[arg-type]
    with pytest.raises(ContinuationStateError):
        AcceptedDocument(TARGET_PATH, b"not text")  # type: ignore[arg-type]
    with pytest.raises(ContinuationStateError):
        RestoredPlan(TARGET_PATH, bytearray(SOURCE), restored.plan)  # type: ignore[arg-type]
