from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import subprocess
import sys
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from ydbdoc_review_ng.domain import (
    DOMAIN_SCHEMA_VERSION,
    ContentHash,
    Diagnostic,
    FilePair,
    GitSha,
    JobId,
    JobResult,
    Locale,
    MergeVerdict,
    Mode,
    ModelRole,
    PublicationDecision,
    RepoPath,
    RepositoryId,
    Severity,
    SnapshotRef,
    from_wire,
    to_wire,
)
from ydbdoc_review_ng.errors import (
    InvariantViolation,
    MalformedPayload,
    SerializationError,
    UnknownDomainType,
    UnsupportedSchemaVersion,
)
from ydbdoc_review_ng.ports import (
    Clock,
    GitHubGateway,
    ModelClient,
    RequestT_contra,
    ResultT_co,
    SnapshotReader,
    StateStore,
)


class StringSubclass(str):
    pass


class TupleSubclass(tuple[object, ...]):
    pass


ENUM_EXPECTATIONS = {
    Locale: {"RU": "ru", "EN": "en"},
    Mode: {
        "DOC_TRANSLATE": "doc_translate",
        "DOC_VERIFY": "doc_verify",
        "DOC_CONTINUE": "doc_continue",
    },
    ModelRole: {
        "DIRECTION": "direction",
        "TRANSLATE": "translate",
        "CRITIC": "critic",
        "REPAIR": "repair",
        "FINAL_CRITIC": "final_critic",
    },
    Severity: {"GREEN": "green", "YELLOW": "yellow", "RED": "red"},
    PublicationDecision: {
        "WITHHOLD_INCOMPLETE": "withhold_incomplete",
        "WITHHOLD_UNSAFE": "withhold_unsafe",
        "PUBLISH_RED": "publish_red",
        "PUBLISH_NORMAL": "publish_normal",
    },
    MergeVerdict: {
        "NOT_APPLICABLE": "not_applicable",
        "BLOCKED": "blocked",
        "WAITING_CI": "waiting_ci",
        "READY": "ready",
    },
}

SCALAR_CASES = [
    (JobId, "job-42"),
    (RepositoryId, "ydb-platform/ydb"),
    (GitSha, "a" * 40),
    (ContentHash, "b" * 64),
    (RepoPath, "ydb/docs/en/index.md"),
]


def make_snapshot() -> SnapshotRef:
    return SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


def make_pair() -> FilePair:
    return FilePair(
        Locale.RU,
        Locale.EN,
        RepoPath("ydb/docs/ru/index.md"),
        RepoPath("ydb/docs/en/index.md"),
    )


def make_diagnostic() -> Diagnostic:
    return Diagnostic(
        Severity.RED,
        "broken_link",
        "Target is missing",
        "Create the target page",
        RepoPath("ydb/docs/en/index.md"),
        3,
        4,
        "[target](missing.md)",
    )


def make_result() -> JobResult:
    return JobResult(
        JobId("job-42"),
        Mode.DOC_TRANSLATE,
        PublicationDecision.PUBLISH_NORMAL,
        MergeVerdict.WAITING_CI,
        make_snapshot(),
        (make_diagnostic(),),
    )


def assert_invariant(
    constructor: Callable[..., object], type_name: str, field_name: str
) -> None:
    with pytest.raises(InvariantViolation) as caught:
        constructor()
    assert type_name in str(caught.value)
    assert field_name in str(caught.value)


def test_a001_enum_vocabulary_is_exact_and_has_no_aliases() -> None:
    for enum_type, expected in ENUM_EXPECTATIONS.items():
        assert issubclass(enum_type, str)
        assert issubclass(enum_type, Enum)
        assert {member.name: member.value for member in enum_type} == expected
        assert len(enum_type.__members__) == len(expected)
        with pytest.raises(ValueError):
            enum_type("UNKNOWN")


@pytest.mark.parametrize(("scalar_type", "value"), SCALAR_CASES)
def test_a002_scalars_accept_valid_values_and_stringify(
    scalar_type: type[Any], value: str
) -> None:
    scalar = scalar_type(value)
    assert scalar.value == value
    assert str(scalar) == value


@pytest.mark.parametrize(("scalar_type", "valid"), SCALAR_CASES)
@pytest.mark.parametrize("wrong", [None, 1, StringSubclass("value")])
def test_a002_scalars_reject_non_exact_string_types(
    scalar_type: type[Any], valid: str, wrong: object
) -> None:
    del valid
    assert_invariant(lambda: scalar_type(wrong), scalar_type.__name__, "value")


@pytest.mark.parametrize(
    ("scalar_type", "invalid"),
    [
        (JobId, ""),
        (JobId, " job"),
        (JobId, "job "),
        (JobId, "job\x00id"),
        (JobId, "job\rid"),
        (JobId, "job\nid"),
        (RepositoryId, "owner"),
        (RepositoryId, "/name"),
        (RepositoryId, "owner/"),
        (RepositoryId, "owner/name/more"),
        (RepositoryId, "owner name/repo"),
        (RepositoryId, "./repo"),
        (RepositoryId, "owner/.."),
        (GitSha, "a" * 39),
        (GitSha, "a" * 41),
        (GitSha, "A" * 40),
        (GitSha, "g" * 40),
        (ContentHash, "b" * 63),
        (ContentHash, "b" * 65),
        (ContentHash, "B" * 64),
        (ContentHash, "z" * 64),
        (RepoPath, ""),
        (RepoPath, "/absolute.md"),
        (RepoPath, "trailing/"),
        (RepoPath, "a\\b.md"),
        (RepoPath, "a//b.md"),
        (RepoPath, "a/./b.md"),
        (RepoPath, "a/../b.md"),
        (RepoPath, "a\x00b.md"),
    ],
)
def test_a002_scalars_reject_invalid_content(
    scalar_type: type[Any], invalid: str
) -> None:
    assert_invariant(lambda: scalar_type(invalid), scalar_type.__name__, "value")


@pytest.mark.parametrize(
    "value",
    [
        JobId("job-42"),
        RepositoryId("ydb-platform/ydb"),
        GitSha("a" * 40),
        ContentHash("b" * 64),
        RepoPath("index.md"),
        make_snapshot(),
        make_pair(),
        make_diagnostic(),
        make_result(),
    ],
)
def test_a003_domain_values_are_frozen_and_slotted(value: object) -> None:
    field_name = dataclasses.fields(value)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field_name, object())
    assert "__dict__" not in dir(value)


RECORD_WRONG_FIELD_CASES = [
    (SnapshotRef, ("ydb-platform/ydb", GitSha("a" * 40)), "repository"),
    (SnapshotRef, (RepositoryId("ydb-platform/ydb"), "a" * 40), "commit_sha"),
    (FilePair, ("ru", Locale.EN, RepoPath("ru.md"), RepoPath("en.md")), "source_locale"),
    (FilePair, (Locale.RU, "en", RepoPath("ru.md"), RepoPath("en.md")), "target_locale"),
    (FilePair, (Locale.RU, Locale.EN, "ru.md", RepoPath("en.md")), "source_path"),
    (FilePair, (Locale.RU, Locale.EN, RepoPath("ru.md"), "en.md"), "target_path"),
    (
        Diagnostic,
        ("red", "code", "message", "action", RepoPath("a.md"), 1, 1, "x"),
        "severity",
    ),
    (Diagnostic, (Severity.RED, StringSubclass("code"), "message", "action", None, None, None, None), "code"),
    (Diagnostic, (Severity.RED, "code", StringSubclass("message"), "action", None, None, None, None), "message"),
    (Diagnostic, (Severity.RED, "code", "message", 3, None, None, None, None), "action"),
    (Diagnostic, (Severity.RED, "code", "message", StringSubclass("action"), None, None, None, None), "action"),
    (Diagnostic, (Severity.RED, "code", "message", "action", "a.md", None, None, None), "path"),
    (Diagnostic, (Severity.RED, "code", "message", "action", RepoPath("a.md"), True, 1, None), "line_start"),
    (Diagnostic, (Severity.RED, "code", "message", "action", RepoPath("a.md"), 1, True, None), "line_end"),
    (Diagnostic, (Severity.RED, "code", "message", "action", RepoPath("a.md"), None, None, 7), "excerpt"),
    (Diagnostic, (Severity.RED, "code", "message", "action", RepoPath("a.md"), None, None, StringSubclass("x")), "excerpt"),
    (JobResult, ("job-42", Mode.DOC_VERIFY, None, MergeVerdict.NOT_APPLICABLE, None, ()), "job_id"),
    (JobResult, (JobId("job-42"), "doc_verify", None, MergeVerdict.NOT_APPLICABLE, None, ()), "mode"),
    (JobResult, (JobId("job-42"), Mode.DOC_VERIFY, "publish_normal", MergeVerdict.BLOCKED, None, ()), "publication_decision"),
    (JobResult, (JobId("job-42"), Mode.DOC_VERIFY, None, "blocked", None, ()), "merge_verdict"),
    (JobResult, (JobId("job-42"), Mode.DOC_VERIFY, None, MergeVerdict.BLOCKED, "snapshot", ()), "checked_snapshot"),
    (JobResult, (JobId("job-42"), Mode.DOC_VERIFY, None, MergeVerdict.BLOCKED, None, []), "diagnostics"),
    (JobResult, (JobId("job-42"), Mode.DOC_VERIFY, None, MergeVerdict.BLOCKED, None, TupleSubclass()), "diagnostics"),
    (JobResult, (JobId("job-42"), Mode.DOC_VERIFY, None, MergeVerdict.BLOCKED, None, (object(),)), "diagnostics"),
]


@pytest.mark.parametrize(("record_type", "args", "field"), RECORD_WRONG_FIELD_CASES)
def test_a003_records_reject_every_wrong_exact_field_type(
    record_type: type[Any], args: tuple[object, ...], field: str
) -> None:
    assert_invariant(lambda: record_type(*args), record_type.__name__, field)


def test_a004_file_pair_accepts_both_directions_without_path_prefix_policy() -> None:
    assert FilePair(Locale.RU, Locale.EN, RepoPath("any/a.md"), RepoPath("other/b.md"))
    assert FilePair(Locale.EN, Locale.RU, RepoPath("any/b.md"), RepoPath("other/a.md"))


@pytest.mark.parametrize(
    "args",
    [
        (Locale.RU, Locale.RU, RepoPath("ru.md"), RepoPath("en.md")),
        (Locale.RU, Locale.EN, RepoPath("same.md"), RepoPath("same.md")),
    ],
)
def test_a004_file_pair_rejects_equal_locales_or_paths(args: tuple[object, ...]) -> None:
    with pytest.raises(InvariantViolation, match="FilePair"):
        FilePair(*args)


def test_a005_diagnostic_accepts_job_and_file_level_shapes() -> None:
    assert Diagnostic(Severity.YELLOW, "direction", "Direction is unclear", "Choose a direction")
    assert make_diagnostic()
    assert Diagnostic(Severity.GREEN, "ok", "No issues")


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ((Severity.RED, "code", "message"), "action"),
        ((Severity.YELLOW, "code", "message", ""), "action"),
        ((Severity.GREEN, "", "message"), "code"),
        ((Severity.GREEN, " code", "message"), "code"),
        ((Severity.GREEN, "code", ""), "message"),
        ((Severity.GREEN, "code", "message", None, RepoPath("a.md"), 1, None), "line_end"),
        ((Severity.GREEN, "code", "message", None, RepoPath("a.md"), None, 1), "line_start"),
        ((Severity.GREEN, "code", "message", None, RepoPath("a.md"), 0, 1), "line_start"),
        ((Severity.GREEN, "code", "message", None, RepoPath("a.md"), 2, 1), "line_start"),
        ((Severity.GREEN, "code", "message", None, None, 1, 1), "path"),
        ((Severity.GREEN, "code", "message", None, None, None, None, "excerpt"), "path"),
        ((Severity.GREEN, "code", "message", None, RepoPath("a.md"), None, None, ""), "excerpt"),
    ],
)
def test_a005_diagnostic_rejects_invalid_invariants(
    args: tuple[object, ...], field: str
) -> None:
    assert_invariant(lambda: Diagnostic(*args), "Diagnostic", field)


@pytest.mark.parametrize(
    ("publication", "verdict", "snapshot"),
    [
        (PublicationDecision.PUBLISH_RED, MergeVerdict.BLOCKED, None),
        (PublicationDecision.PUBLISH_NORMAL, MergeVerdict.WAITING_CI, make_snapshot()),
        (PublicationDecision.PUBLISH_NORMAL, MergeVerdict.READY, make_snapshot()),
        (None, MergeVerdict.NOT_APPLICABLE, None),
        (None, MergeVerdict.READY, make_snapshot()),
    ],
)
def test_a006_job_result_represents_publication_and_merge_independently(
    publication: PublicationDecision | None,
    verdict: MergeVerdict,
    snapshot: SnapshotRef | None,
) -> None:
    assert JobResult(JobId("job-42"), Mode.DOC_VERIFY, publication, verdict, snapshot, ())


@pytest.mark.parametrize("verdict", [MergeVerdict.READY, MergeVerdict.WAITING_CI])
def test_a006_job_result_requires_snapshot_for_checked_verdicts(verdict: MergeVerdict) -> None:
    assert_invariant(
        lambda: JobResult(JobId("job-42"), Mode.DOC_VERIFY, None, verdict, None, ()),
        "JobResult",
        "checked_snapshot",
    )


@pytest.mark.parametrize(
    "publication",
    [
        PublicationDecision.PUBLISH_RED,
        PublicationDecision.WITHHOLD_INCOMPLETE,
        PublicationDecision.WITHHOLD_UNSAFE,
    ],
)
def test_a006_job_result_rejects_forbidden_ready_publication(
    publication: PublicationDecision,
) -> None:
    assert_invariant(
        lambda: JobResult(
            JobId("job-42"),
            Mode.DOC_VERIFY,
            publication,
            MergeVerdict.READY,
            make_snapshot(),
            (),
        ),
        "JobResult",
        "publication_decision",
    )


SERIALIZABLE_SAMPLES = [
    (Locale, Locale.RU, "locale"),
    (Mode, Mode.DOC_VERIFY, "mode"),
    (ModelRole, ModelRole.TRANSLATE, "model_role"),
    (Severity, Severity.YELLOW, "severity"),
    (PublicationDecision, PublicationDecision.PUBLISH_NORMAL, "publication_decision"),
    (MergeVerdict, MergeVerdict.BLOCKED, "merge_verdict"),
    (JobId, JobId("job-42"), "job_id"),
    (RepositoryId, RepositoryId("ydb-platform/ydb"), "repository_id"),
    (GitSha, GitSha("a" * 40), "git_sha"),
    (ContentHash, ContentHash("b" * 64), "content_hash"),
    (RepoPath, RepoPath("index.md"), "repo_path"),
    (SnapshotRef, make_snapshot(), "snapshot_ref"),
    (FilePair, make_pair(), "file_pair"),
    (Diagnostic, make_diagnostic(), "diagnostic"),
    (JobResult, make_result(), "job_result"),
]


@pytest.mark.parametrize(("value_type", "sample", "tag"), SERIALIZABLE_SAMPLES)
def test_a007_all_fifteen_tags_round_trip(
    value_type: type[Any], sample: object, tag: str
) -> None:
    payload = to_wire(sample)
    assert payload["schema_version"] == DOMAIN_SCHEMA_VERSION == 1
    assert payload["type"] == tag
    assert from_wire(value_type, payload) == sample


@pytest.mark.parametrize("enum_type", list(ENUM_EXPECTATIONS))
def test_a007_every_enum_member_round_trips(enum_type: type[Enum]) -> None:
    for member in enum_type:
        assert from_wire(enum_type, to_wire(member)) == member


def test_a008_record_wire_shapes_are_exact() -> None:
    assert to_wire(make_snapshot()) == {
        "schema_version": 1,
        "type": "snapshot_ref",
        "data": {"repository": "ydb-platform/ydb", "commit_sha": "a" * 40},
    }
    assert to_wire(make_pair()) == {
        "schema_version": 1,
        "type": "file_pair",
        "data": {
            "source_locale": "ru",
            "target_locale": "en",
            "source_path": "ydb/docs/ru/index.md",
            "target_path": "ydb/docs/en/index.md",
        },
    }
    assert to_wire(Diagnostic(Severity.GREEN, "ok", "No issues")) == {
        "schema_version": 1,
        "type": "diagnostic",
        "data": {
            "severity": "green",
            "code": "ok",
            "message": "No issues",
            "action": None,
            "path": None,
            "line_start": None,
            "line_end": None,
            "excerpt": None,
        },
    }
    assert to_wire(make_result()) == {
        "schema_version": 1,
        "type": "job_result",
        "data": {
            "job_id": "job-42",
            "mode": "doc_translate",
            "publication_decision": "publish_normal",
            "merge_verdict": "waiting_ci",
            "checked_snapshot": {"repository": "ydb-platform/ydb", "commit_sha": "a" * 40},
            "diagnostics": [
                {
                    "severity": "red",
                    "code": "broken_link",
                    "message": "Target is missing",
                    "action": "Create the target page",
                    "path": "ydb/docs/en/index.md",
                    "line_start": 3,
                    "line_end": 4,
                    "excerpt": "[target](missing.md)",
                }
            ],
        },
    }


def test_a008_job_result_null_optionals_have_exact_wire_shape_and_round_trip() -> None:
    result = JobResult(
        JobId("job-no-publication"),
        Mode.DOC_VERIFY,
        None,
        MergeVerdict.NOT_APPLICABLE,
        None,
        (),
    )
    expected = {
        "schema_version": 1,
        "type": "job_result",
        "data": {
            "job_id": "job-no-publication",
            "mode": "doc_verify",
            "publication_decision": None,
            "merge_verdict": "not_applicable",
            "checked_snapshot": None,
            "diagnostics": [],
        },
    }

    payload = to_wire(result)

    assert payload == expected
    assert set(payload["data"]) == {
        "job_id",
        "mode",
        "publication_decision",
        "merge_verdict",
        "checked_snapshot",
        "diagnostics",
    }
    assert payload["data"]["publication_decision"] is None
    assert payload["data"]["checked_snapshot"] is None
    assert from_wire(JobResult, expected) == result


@pytest.mark.parametrize(("value_type", "sample", "tag"), SERIALIZABLE_SAMPLES)
def test_a009_every_tag_rejects_non_exact_envelope_and_wrong_data_kind(
    value_type: type[Any], sample: object, tag: str
) -> None:
    good = to_wire(sample)
    missing_data = {"schema_version": 1, "type": tag}
    extra = {**good, "extra": "sentinel-secret"}
    wrong_data = {**good, "data": {} if not isinstance(good["data"], dict) else "wrong"}
    for payload in (missing_data, extra, wrong_data):
        with pytest.raises(MalformedPayload) as caught:
            from_wire(value_type, payload)
        assert "$" in str(caught.value)
        assert "expected" in str(caught.value)
        assert "sentinel-secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("value_type", "sample", "field"),
    [
        (SnapshotRef, make_snapshot(), "repository"),
        (SnapshotRef, make_snapshot(), "commit_sha"),
        (FilePair, make_pair(), "source_locale"),
        (FilePair, make_pair(), "target_locale"),
        (FilePair, make_pair(), "source_path"),
        (FilePair, make_pair(), "target_path"),
        *[(Diagnostic, make_diagnostic(), field) for field in (
            "severity", "code", "message", "action", "path", "line_start", "line_end", "excerpt"
        )],
        *[(JobResult, make_result(), field) for field in (
            "job_id", "mode", "publication_decision", "merge_verdict", "checked_snapshot", "diagnostics"
        )],
    ],
)
def test_a009_each_record_field_rejects_missing_extra_and_wrong_json_type(
    value_type: type[Any], sample: object, field: str
) -> None:
    good = to_wire(sample)
    assert isinstance(good["data"], dict)
    missing = deepcopy(good)
    del missing["data"][field]
    extra = deepcopy(good)
    extra["data"]["extra"] = "sentinel-secret"
    wrong = deepcopy(good)
    current = wrong["data"][field]
    if field in {"line_start", "line_end"}:
        wrong["data"][field] = True
    elif field == "diagnostics":
        wrong["data"][field] = ["wrong"]
    elif isinstance(current, dict):
        wrong["data"][field] = "wrong"
    else:
        wrong["data"][field] = 123
    for payload in (missing, extra, wrong):
        with pytest.raises(MalformedPayload) as caught:
            from_wire(value_type, payload)
        assert "$.data" in str(caught.value)
        assert "expected" in str(caught.value)
        assert "sentinel-secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("payload", "error_type", "path"),
    [
        ({"type": "locale", "data": "ru"}, UnsupportedSchemaVersion, "$.schema_version"),
        ({"schema_version": 2, "type": "locale", "data": "ru"}, UnsupportedSchemaVersion, "$.schema_version"),
        ({"schema_version": True, "type": "locale", "data": "ru"}, UnsupportedSchemaVersion, "$.schema_version"),
        ({"schema_version": "1", "type": "locale", "data": "ru"}, UnsupportedSchemaVersion, "$.schema_version"),
        ({"schema_version": 1, "data": "ru"}, MalformedPayload, "$.type"),
        ({"schema_version": 1, "type": 3, "data": "ru"}, MalformedPayload, "$.type"),
        ({"schema_version": 1, "type": "unknown", "data": "ru"}, UnknownDomainType, "$.type"),
        ({"schema_version": 1, "type": "mode", "data": "ru"}, UnknownDomainType, "$.type"),
        ({"schema_version": 1, "type": "locale", "data": "unknown"}, MalformedPayload, "$.data"),
    ],
)
def test_a009_envelope_error_classification_is_exact(
    payload: dict[str, object], error_type: type[SerializationError], path: str
) -> None:
    if "schema_version" not in payload:
        payload = {**payload, "secret": "sentinel-secret"}
    with pytest.raises(error_type) as caught:
        from_wire(Locale, payload)
    assert type(caught.value) is error_type
    assert path in str(caught.value)
    assert "expected" in str(caught.value)
    assert "sentinel-secret" not in str(caught.value)


def test_a009_non_object_payload_and_unknown_python_types_are_classified() -> None:
    with pytest.raises(MalformedPayload, match=r"\$.*expected"):
        from_wire(Locale, [1, 2])  # type: ignore[arg-type]
    with pytest.raises(UnknownDomainType, match=r"\$.*expected"):
        from_wire(bytes, {"schema_version": 1, "type": "locale", "data": "ru"})
    with pytest.raises(UnknownDomainType, match=r"\$.*expected"):
        to_wire(object())  # type: ignore[arg-type]


def test_a009_decode_preserves_enum_and_invariant_causes_without_payload_echo() -> None:
    with pytest.raises(MalformedPayload) as enum_error:
        from_wire(Locale, {"schema_version": 1, "type": "locale", "data": "sentinel-secret"})
    assert type(enum_error.value.__cause__) is ValueError
    assert "sentinel-secret" not in str(enum_error.value)

    payload = to_wire(make_pair())
    assert isinstance(payload["data"], dict)
    payload["data"]["target_locale"] = "ru"
    with pytest.raises(MalformedPayload) as invariant_error:
        from_wire(FilePair, payload)
    assert type(invariant_error.value.__cause__) is InvariantViolation
    assert "$.data.target_locale" in str(invariant_error.value)


def test_a009_job_result_nested_snapshot_error_has_exact_path_kind_and_cause() -> None:
    payload = to_wire(make_result())
    assert isinstance(payload["data"], dict)
    checked_snapshot = payload["data"]["checked_snapshot"]
    assert isinstance(checked_snapshot, dict)
    checked_snapshot["commit_sha"] = "invalid-sha"

    with pytest.raises(MalformedPayload) as caught:
        from_wire(JobResult, payload)

    assert str(caught.value) == (
        "$.data.checked_snapshot.commit_sha: expected valid GitSha string"
    )
    assert type(caught.value.__cause__) is InvariantViolation


def test_a009_job_result_diagnostic_error_has_exact_indexed_path_kind_and_cause() -> None:
    payload = to_wire(make_result())
    assert isinstance(payload["data"], dict)
    diagnostics = payload["data"]["diagnostics"]
    assert isinstance(diagnostics, list)
    diagnostic = diagnostics[0]
    assert isinstance(diagnostic, dict)
    diagnostic["code"] = ""

    with pytest.raises(MalformedPayload) as caught:
        from_wire(JobResult, payload)

    assert str(caught.value) == (
        "$.data.diagnostics[0].code: expected valid Diagnostic.code"
    )
    assert type(caught.value.__cause__) is InvariantViolation


def test_a009_job_result_rejects_non_array_diagnostics_at_exact_path() -> None:
    payload = to_wire(make_result())
    assert isinstance(payload["data"], dict)
    payload["data"]["diagnostics"] = {"not": "an array"}

    with pytest.raises(MalformedPayload) as caught:
        from_wire(JobResult, payload)

    assert type(caught.value) is MalformedPayload
    assert str(caught.value) == "$.data.diagnostics: expected JSON array"


def test_a009_public_serialization_never_raises_base_error_directly() -> None:
    operations = [
        lambda: to_wire(object()),  # type: ignore[arg-type]
        lambda: from_wire(bytes, {}),
        lambda: from_wire(Locale, {}),
        lambda: from_wire(Locale, {"schema_version": 1, "type": "locale", "data": 1}),
    ]
    for operation in operations:
        with pytest.raises(SerializationError) as caught:
            operation()
        assert type(caught.value) is not SerializationError


def test_a010_snapshot_reader_distinguishes_missing_from_empty() -> None:
    class FakeSnapshotReader:
        def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
            assert snapshot == make_snapshot()
            return {"empty.md": b""}.get(path.value)

    reader: SnapshotReader = FakeSnapshotReader()
    assert reader.read_bytes(make_snapshot(), RepoPath("empty.md")) == b""
    assert reader.read_bytes(make_snapshot(), RepoPath("missing.md")) is None


def test_a010_all_protocol_signatures_are_exact() -> None:
    positional_only = inspect.Parameter.POSITIONAL_ONLY
    expected = {
        SnapshotReader.read_bytes: inspect.Signature(
            parameters=[
                inspect.Parameter("self", positional_only),
                inspect.Parameter("snapshot", positional_only, annotation=SnapshotRef),
                inspect.Parameter("path", positional_only, annotation=RepoPath),
            ],
            return_annotation=bytes | None,
        ),
        ModelClient.invoke: inspect.Signature(
            parameters=[
                inspect.Parameter("self", positional_only),
                inspect.Parameter("request", positional_only, annotation=RequestT_contra),
            ],
            return_annotation=ResultT_co,
        ),
        StateStore.execute: inspect.Signature(
            parameters=[
                inspect.Parameter("self", positional_only),
                inspect.Parameter("request", positional_only, annotation=RequestT_contra),
            ],
            return_annotation=ResultT_co,
        ),
        GitHubGateway.execute: inspect.Signature(
            parameters=[
                inspect.Parameter("self", positional_only),
                inspect.Parameter("request", positional_only, annotation=RequestT_contra),
            ],
            return_annotation=ResultT_co,
        ),
        Clock.now: inspect.Signature(
            parameters=[inspect.Parameter("self", positional_only)],
            return_annotation=datetime,
        ),
    }

    for method, expected_signature in expected.items():
        assert inspect.signature(method) == expected_signature


def run_mypy(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    fixture = tmp_path / "typing_fixture.py"
    fixture.write_text(source, encoding="utf-8")
    environment = os.environ.copy()
    environment["MYPYPATH"] = str(Path.cwd() / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            f"--cache-dir={tmp_path / 'mypy-cache'}",
            str(fixture),
        ],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


POSITIVE_MYPY_FIXTURE = '''
from dataclasses import dataclass
from datetime import UTC, datetime
from ydbdoc_review_ng.domain import RepoPath, SnapshotRef
from ydbdoc_review_ng.ports import Clock, GitHubGateway, ModelClient, SnapshotReader, StateStore

@dataclass(frozen=True, slots=True)
class FakeRequest:
    value: str

@dataclass(frozen=True, slots=True)
class FakeResult:
    value: str

class FakeSnapshotReader:
    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
        return b""

class FakeModelClient:
    def invoke(self, request: FakeRequest, /) -> FakeResult:
        return FakeResult(request.value)

class FakeStateStore:
    def execute(self, request: FakeRequest, /) -> FakeResult:
        return FakeResult(request.value)

class FakeGitHubGateway:
    def execute(self, request: FakeRequest, /) -> FakeResult:
        return FakeResult(request.value)

class FakeClock:
    def now(self, /) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

snapshot_reader: SnapshotReader = FakeSnapshotReader()
model_client: ModelClient[FakeRequest, FakeResult] = FakeModelClient()
state_store: StateStore[FakeRequest, FakeResult] = FakeStateStore()
github_gateway: GitHubGateway[FakeRequest, FakeResult] = FakeGitHubGateway()
clock: Clock = FakeClock()
'''


def test_a011_all_five_protocols_accept_valid_structural_implementations(tmp_path: Path) -> None:
    result = run_mypy(tmp_path, POSITIVE_MYPY_FIXTURE)
    assert result.returncode == 0, result.stdout + result.stderr


NEGATIVE_MYPY_FIXTURE = '''
from datetime import date
from ydbdoc_review_ng.domain import RepoPath, SnapshotRef
from ydbdoc_review_ng.ports import Clock, GitHubGateway, ModelClient, SnapshotReader, StateStore

class FakeRequest: pass
class FakeResult: pass

class BadSnapshotReader:
    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> str: return ""
class BadModelClient:
    def invoke(self, request: FakeRequest, /) -> object: return object()
class BadStateStore:
    def execute(self, request: FakeRequest, /) -> object: return object()
class BadGitHubGateway:
    def execute(self, request: FakeRequest, /) -> object: return object()
class BadClock:
    def now(self, /) -> date: return date.today()

bad_snapshot_reader: SnapshotReader = BadSnapshotReader()
bad_model_client: ModelClient[FakeRequest, FakeResult] = BadModelClient()
bad_state_store: StateStore[FakeRequest, FakeResult] = BadStateStore()
bad_github_gateway: GitHubGateway[FakeRequest, FakeResult] = BadGitHubGateway()
bad_clock: Clock = BadClock()
'''


def test_a012_all_five_protocol_mutations_are_rejected_by_mypy(tmp_path: Path) -> None:
    result = run_mypy(tmp_path, NEGATIVE_MYPY_FIXTURE)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    for protocol, method in [
        ("SnapshotReader", "read_bytes"),
        ("ModelClient", "invoke"),
        ("StateStore", "execute"),
        ("GitHubGateway", "execute"),
        ("Clock", "now"),
    ]:
        assert protocol in output
        assert method in output


def test_a013_protocol_public_methods_and_import_boundaries_are_exact() -> None:
    expected_methods = {
        SnapshotReader: {"read_bytes"},
        ModelClient: {"invoke"},
        StateStore: {"execute"},
        GitHubGateway: {"execute"},
        Clock: {"now"},
    }
    for protocol, expected in expected_methods.items():
        public = {
            name
            for name, member in protocol.__dict__.items()
            if callable(member) and not name.startswith("_")
        }
        assert public == expected

    project_root = Path(__file__).parents[2]
    allowed_imports = {
        "src/ydbdoc_review_ng/domain.py": {
            "__future__",
            "collections.abc",
            "dataclasses",
            "enum",
            "re",
            "typing",
            "ydbdoc_review_ng.errors",
        },
        "src/ydbdoc_review_ng/ports.py": {
            "datetime",
            "typing",
            "ydbdoc_review_ng.domain",
        },
        "src/ydbdoc_review_ng/errors.py": set(),
    }
    for relative, allowed in allowed_imports.items():
        tree = ast.parse((project_root / relative).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert imported == allowed


def test_a013_clock_fake_returns_timezone_aware_utc() -> None:
    class FakeClock:
        def now(self, /) -> datetime:
            return datetime(2026, 1, 1, tzinfo=UTC)

    clock: Clock = FakeClock()
    assert clock.now().utcoffset() is not None
    assert clock.now().utcoffset().total_seconds() == 0
