"""Frozen domain vocabulary and its versioned wire representation."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn, TypeAlias, TypeVar, cast

from ydbdoc_review_ng.errors import (
    InvariantViolation,
    MalformedPayload,
    UnknownDomainType,
    UnsupportedSchemaVersion,
)

__all__ = [
    "DOMAIN_SCHEMA_VERSION",
    "ContentHash",
    "Diagnostic",
    "FilePair",
    "GitSha",
    "JobId",
    "JobResult",
    "JsonValue",
    "Locale",
    "MergeVerdict",
    "Mode",
    "ModelRole",
    "PublicationDecision",
    "RepoPath",
    "RepositoryId",
    "SerializableDomain",
    "Severity",
    "SnapshotRef",
    "from_wire",
    "to_wire",
]

DOMAIN_SCHEMA_VERSION = 1

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class Locale(str, Enum):
    RU = "ru"
    EN = "en"


class Mode(str, Enum):
    DOC_TRANSLATE = "doc_translate"
    DOC_VERIFY = "doc_verify"
    DOC_CONTINUE = "doc_continue"


class ModelRole(str, Enum):
    DIRECTION = "direction"
    TRANSLATE = "translate"
    CRITIC = "critic"
    REPAIR = "repair"
    FINAL_CRITIC = "final_critic"


class Severity(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class PublicationDecision(str, Enum):
    WITHHOLD_INCOMPLETE = "withhold_incomplete"
    WITHHOLD_UNSAFE = "withhold_unsafe"
    PUBLISH_RED = "publish_red"
    PUBLISH_NORMAL = "publish_normal"


class MergeVerdict(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"
    WAITING_CI = "waiting_ci"
    READY = "ready"


def _invariant(type_name: str, field_name: str, expectation: str) -> InvariantViolation:
    return InvariantViolation(f"{type_name}.{field_name}: expected {expectation}")


def _require_field_type(
    value: object, expected_type: type[object], type_name: str, field_name: str
) -> None:
    if type(value) is not expected_type:
        raise _invariant(type_name, field_name, f"exact {expected_type.__name__}")


def _require_optional_field_type(
    value: object, expected_type: type[object], type_name: str, field_name: str
) -> None:
    if value is not None and type(value) is not expected_type:
        raise _invariant(type_name, field_name, f"None or exact {expected_type.__name__}")


def _require_scalar_string(value: object, type_name: str) -> None:
    _require_field_type(value, str, type_name, "value")


@dataclass(frozen=True, slots=True)
class JobId:
    value: str

    def __post_init__(self) -> None:
        _require_scalar_string(self.value, "JobId")
        if not self.value or self.value != self.value.strip() or any(
            character in self.value for character in "\x00\r\n"
        ):
            raise _invariant("JobId", "value", "a non-empty trimmed identifier without NUL/CR/LF")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class RepositoryId:
    value: str

    def __post_init__(self) -> None:
        _require_scalar_string(self.value, "RepositoryId")
        components = self.value.split("/")
        if (
            len(components) != 2
            or any(not component or component in {".", ".."} for component in components)
            or any(character.isspace() for character in self.value)
        ):
            raise _invariant("RepositoryId", "value", "exact owner/name without whitespace")

    def __str__(self) -> str:
        return self.value


_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_CONTENT_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class GitSha:
    value: str

    def __post_init__(self) -> None:
        _require_scalar_string(self.value, "GitSha")
        if _GIT_SHA_PATTERN.fullmatch(self.value) is None:
            raise _invariant("GitSha", "value", "40 lowercase hexadecimal characters")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ContentHash:
    value: str

    def __post_init__(self) -> None:
        _require_scalar_string(self.value, "ContentHash")
        if _CONTENT_HASH_PATTERN.fullmatch(self.value) is None:
            raise _invariant("ContentHash", "value", "64 lowercase hexadecimal characters")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class RepoPath:
    value: str

    def __post_init__(self) -> None:
        _require_scalar_string(self.value, "RepoPath")
        components = self.value.split("/")
        if (
            not self.value
            or self.value.startswith("/")
            or self.value.endswith("/")
            or "\\" in self.value
            or "\x00" in self.value
            or any(component in {"", ".", ".."} for component in components)
        ):
            raise _invariant("RepoPath", "value", "a non-empty relative POSIX path without traversal")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    repository: RepositoryId
    commit_sha: GitSha

    def __post_init__(self) -> None:
        _require_field_type(self.repository, RepositoryId, "SnapshotRef", "repository")
        _require_field_type(self.commit_sha, GitSha, "SnapshotRef", "commit_sha")


@dataclass(frozen=True, slots=True)
class FilePair:
    source_locale: Locale
    target_locale: Locale
    source_path: RepoPath
    target_path: RepoPath

    def __post_init__(self) -> None:
        _require_field_type(self.source_locale, Locale, "FilePair", "source_locale")
        _require_field_type(self.target_locale, Locale, "FilePair", "target_locale")
        _require_field_type(self.source_path, RepoPath, "FilePair", "source_path")
        _require_field_type(self.target_path, RepoPath, "FilePair", "target_path")
        if self.source_locale == self.target_locale:
            raise _invariant("FilePair", "target_locale", "a locale different from source_locale")
        if self.source_path == self.target_path:
            raise _invariant("FilePair", "target_path", "a path different from source_path")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    action: str | None = None
    path: RepoPath | None = None
    line_start: int | None = None
    line_end: int | None = None
    excerpt: str | None = None

    def __post_init__(self) -> None:
        _require_field_type(self.severity, Severity, "Diagnostic", "severity")
        _require_field_type(self.code, str, "Diagnostic", "code")
        _require_field_type(self.message, str, "Diagnostic", "message")
        _require_optional_field_type(self.action, str, "Diagnostic", "action")
        _require_optional_field_type(self.path, RepoPath, "Diagnostic", "path")
        _require_optional_field_type(self.line_start, int, "Diagnostic", "line_start")
        _require_optional_field_type(self.line_end, int, "Diagnostic", "line_end")
        _require_optional_field_type(self.excerpt, str, "Diagnostic", "excerpt")

        if not self.code or self.code != self.code.strip():
            raise _invariant("Diagnostic", "code", "a non-empty trimmed string")
        if not self.message or self.message != self.message.strip():
            raise _invariant("Diagnostic", "message", "a non-empty trimmed string")
        if self.severity in {Severity.YELLOW, Severity.RED} and (
            self.action is None or not self.action.strip()
        ):
            raise _invariant("Diagnostic", "action", "a non-empty action for YELLOW or RED")
        if (self.line_start is None) != (self.line_end is None):
            missing_field = "line_start" if self.line_start is None else "line_end"
            raise _invariant("Diagnostic", missing_field, "both line fields or neither")
        if self.line_start is not None and self.line_end is not None:
            if self.line_start < 1 or self.line_start > self.line_end:
                raise _invariant("Diagnostic", "line_start", "1 <= line_start <= line_end")
            if self.path is None:
                raise _invariant("Diagnostic", "path", "a path when line fields are present")
        if self.excerpt is not None:
            if not self.excerpt:
                raise _invariant("Diagnostic", "excerpt", "a non-empty string")
            if self.path is None:
                raise _invariant("Diagnostic", "path", "a path when excerpt is present")


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: JobId
    mode: Mode
    publication_decision: PublicationDecision | None
    merge_verdict: MergeVerdict
    checked_snapshot: SnapshotRef | None
    diagnostics: tuple[Diagnostic, ...]

    def __post_init__(self) -> None:
        _require_field_type(self.job_id, JobId, "JobResult", "job_id")
        _require_field_type(self.mode, Mode, "JobResult", "mode")
        _require_optional_field_type(
            self.publication_decision, PublicationDecision, "JobResult", "publication_decision"
        )
        _require_field_type(self.merge_verdict, MergeVerdict, "JobResult", "merge_verdict")
        _require_optional_field_type(
            self.checked_snapshot, SnapshotRef, "JobResult", "checked_snapshot"
        )
        _require_field_type(self.diagnostics, tuple, "JobResult", "diagnostics")
        if any(type(item) is not Diagnostic for item in self.diagnostics):
            raise _invariant("JobResult", "diagnostics", "a tuple containing exact Diagnostic values")

        if self.merge_verdict in {MergeVerdict.READY, MergeVerdict.WAITING_CI} and (
            self.checked_snapshot is None
        ):
            raise _invariant("JobResult", "checked_snapshot", "a snapshot for READY or WAITING_CI")
        if self.merge_verdict is MergeVerdict.READY and self.publication_decision in {
            PublicationDecision.PUBLISH_RED,
            PublicationDecision.WITHHOLD_INCOMPLETE,
            PublicationDecision.WITHHOLD_UNSAFE,
        }:
            raise _invariant("JobResult", "publication_decision", "a decision compatible with READY")


SerializableDomain: TypeAlias = (
    Locale
    | Mode
    | ModelRole
    | Severity
    | PublicationDecision
    | MergeVerdict
    | JobId
    | RepositoryId
    | GitSha
    | ContentHash
    | RepoPath
    | SnapshotRef
    | FilePair
    | Diagnostic
    | JobResult
)

T = TypeVar("T", bound=SerializableDomain)

_TYPE_TO_TAG: dict[type[object], str] = {
    Locale: "locale",
    Mode: "mode",
    ModelRole: "model_role",
    Severity: "severity",
    PublicationDecision: "publication_decision",
    MergeVerdict: "merge_verdict",
    JobId: "job_id",
    RepositoryId: "repository_id",
    GitSha: "git_sha",
    ContentHash: "content_hash",
    RepoPath: "repo_path",
    SnapshotRef: "snapshot_ref",
    FilePair: "file_pair",
    Diagnostic: "diagnostic",
    JobResult: "job_result",
}

_ENUM_TYPES: set[type[object]] = {
    Locale,
    Mode,
    ModelRole,
    Severity,
    PublicationDecision,
    MergeVerdict,
}
_SCALAR_TYPES: set[type[object]] = {JobId, RepositoryId, GitSha, ContentHash, RepoPath}


def _snapshot_data(value: SnapshotRef) -> dict[str, JsonValue]:
    return {"repository": value.repository.value, "commit_sha": value.commit_sha.value}


def _diagnostic_data(value: Diagnostic) -> dict[str, JsonValue]:
    return {
        "severity": value.severity.value,
        "code": value.code,
        "message": value.message,
        "action": value.action,
        "path": None if value.path is None else value.path.value,
        "line_start": value.line_start,
        "line_end": value.line_end,
        "excerpt": value.excerpt,
    }


def to_wire(value: SerializableDomain) -> dict[str, JsonValue]:
    """Encode one supported domain value using schema version 1."""
    value_type = type(value)
    tag = _TYPE_TO_TAG.get(value_type)
    if tag is None:
        raise UnknownDomainType("$: expected a supported exact domain type")

    data: JsonValue
    if value_type in _ENUM_TYPES:
        data = cast(Enum, value).value
    elif value_type in _SCALAR_TYPES:
        data = cast(JobId | RepositoryId | GitSha | ContentHash | RepoPath, value).value
    elif value_type is SnapshotRef:
        data = _snapshot_data(cast(SnapshotRef, value))
    elif value_type is FilePair:
        pair = cast(FilePair, value)
        data = {
            "source_locale": pair.source_locale.value,
            "target_locale": pair.target_locale.value,
            "source_path": pair.source_path.value,
            "target_path": pair.target_path.value,
        }
    elif value_type is Diagnostic:
        data = _diagnostic_data(cast(Diagnostic, value))
    elif value_type is JobResult:
        result = cast(JobResult, value)
        data = {
            "job_id": result.job_id.value,
            "mode": result.mode.value,
            "publication_decision": (
                None if result.publication_decision is None else result.publication_decision.value
            ),
            "merge_verdict": result.merge_verdict.value,
            "checked_snapshot": (
                None if result.checked_snapshot is None else _snapshot_data(result.checked_snapshot)
            ),
            "diagnostics": [_diagnostic_data(item) for item in result.diagnostics],
        }
    else:  # pragma: no cover - guarded by the exact type registry above
        raise UnknownDomainType("$: expected a supported exact domain type")
    return {"schema_version": DOMAIN_SCHEMA_VERSION, "type": tag, "data": data}


def _malformed(path: str, expectation: str) -> MalformedPayload:
    return MalformedPayload(f"{path}: expected {expectation}")


def _raise_malformed_invariant(path: str, error: InvariantViolation) -> NoReturn:
    type_and_field = str(error).partition(":")[0]
    field = type_and_field.partition(".")[2]
    error_path = f"{path}.{field}" if field else path
    raise _malformed(error_path, f"valid {type_and_field}") from error


def _require_object(value: object, path: str) -> dict[str, object]:
    if type(value) is not dict:
        raise _malformed(path, "JSON object")
    return cast(dict[str, object], value)


def _require_string(value: object, path: str) -> str:
    if type(value) is not str:
        raise _malformed(path, "JSON string")
    return value


def _require_optional_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, path)


def _require_optional_int(value: object, path: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise _malformed(path, "JSON integer or null")
    return value


def _require_exact_keys(data: Mapping[str, object], keys: set[str], path: str) -> None:
    if set(data) != keys:
        raise _malformed(path, f"exact keys {', '.join(sorted(keys))}")


def _construct_string_value(value_type: type[T], value: object, path: str) -> T:
    string = _require_string(value, path)
    constructor = cast(Callable[[str], T], value_type)
    try:
        return constructor(string)
    except (ValueError, InvariantViolation) as error:
        raise _malformed(path, f"valid {value_type.__name__} string") from error


def _decode_snapshot(value: object, path: str) -> SnapshotRef:
    data = _require_object(value, path)
    _require_exact_keys(data, {"repository", "commit_sha"}, path)
    repository = _construct_string_value(RepositoryId, data["repository"], f"{path}.repository")
    commit_sha = _construct_string_value(GitSha, data["commit_sha"], f"{path}.commit_sha")
    try:
        return SnapshotRef(repository, commit_sha)
    except InvariantViolation as error:
        _raise_malformed_invariant(path, error)


def _decode_file_pair(value: object, path: str) -> FilePair:
    data = _require_object(value, path)
    keys = {"source_locale", "target_locale", "source_path", "target_path"}
    _require_exact_keys(data, keys, path)
    source_locale = _construct_string_value(Locale, data["source_locale"], f"{path}.source_locale")
    target_locale = _construct_string_value(Locale, data["target_locale"], f"{path}.target_locale")
    source_path = _construct_string_value(RepoPath, data["source_path"], f"{path}.source_path")
    target_path = _construct_string_value(RepoPath, data["target_path"], f"{path}.target_path")
    try:
        return FilePair(source_locale, target_locale, source_path, target_path)
    except InvariantViolation as error:
        _raise_malformed_invariant(path, error)


_DIAGNOSTIC_KEYS = {
    "severity",
    "code",
    "message",
    "action",
    "path",
    "line_start",
    "line_end",
    "excerpt",
}


def _decode_diagnostic(value: object, path: str) -> Diagnostic:
    data = _require_object(value, path)
    _require_exact_keys(data, _DIAGNOSTIC_KEYS, path)
    severity = _construct_string_value(Severity, data["severity"], f"{path}.severity")
    code = _require_string(data["code"], f"{path}.code")
    message = _require_string(data["message"], f"{path}.message")
    action = _require_optional_string(data["action"], f"{path}.action")
    raw_path = _require_optional_string(data["path"], f"{path}.path")
    repo_path = (
        None if raw_path is None else _construct_string_value(RepoPath, raw_path, f"{path}.path")
    )
    line_start = _require_optional_int(data["line_start"], f"{path}.line_start")
    line_end = _require_optional_int(data["line_end"], f"{path}.line_end")
    excerpt = _require_optional_string(data["excerpt"], f"{path}.excerpt")
    try:
        return Diagnostic(severity, code, message, action, repo_path, line_start, line_end, excerpt)
    except InvariantViolation as error:
        _raise_malformed_invariant(path, error)


_JOB_RESULT_KEYS = {
    "job_id",
    "mode",
    "publication_decision",
    "merge_verdict",
    "checked_snapshot",
    "diagnostics",
}


def _decode_job_result(value: object, path: str) -> JobResult:
    data = _require_object(value, path)
    _require_exact_keys(data, _JOB_RESULT_KEYS, path)
    job_id = _construct_string_value(JobId, data["job_id"], f"{path}.job_id")
    mode = _construct_string_value(Mode, data["mode"], f"{path}.mode")
    raw_publication = _require_optional_string(
        data["publication_decision"], f"{path}.publication_decision"
    )
    publication = (
        None
        if raw_publication is None
        else _construct_string_value(
            PublicationDecision, raw_publication, f"{path}.publication_decision"
        )
    )
    merge_verdict = _construct_string_value(
        MergeVerdict, data["merge_verdict"], f"{path}.merge_verdict"
    )
    checked_snapshot = (
        None
        if data["checked_snapshot"] is None
        else _decode_snapshot(data["checked_snapshot"], f"{path}.checked_snapshot")
    )
    raw_diagnostics = data["diagnostics"]
    if type(raw_diagnostics) is not list:
        raise _malformed(f"{path}.diagnostics", "JSON array")
    diagnostics = tuple(
        _decode_diagnostic(item, f"{path}.diagnostics[{index}]")
        for index, item in enumerate(cast(list[object], raw_diagnostics))
    )
    try:
        return JobResult(job_id, mode, publication, merge_verdict, checked_snapshot, diagnostics)
    except InvariantViolation as error:
        _raise_malformed_invariant(path, error)


def from_wire(expected_type: type[T], payload: Mapping[str, object]) -> T:
    """Decode a schema-version-1 payload as one exact supported domain type."""
    expected_tag = _TYPE_TO_TAG.get(expected_type)
    if expected_tag is None:
        raise UnknownDomainType("$: expected a supported exact domain type")
    if not isinstance(payload, Mapping):
        raise _malformed("$", "JSON object")
    if "schema_version" not in payload:
        raise UnsupportedSchemaVersion("$.schema_version: expected integer schema version 1")
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != DOMAIN_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion("$.schema_version: expected integer schema version 1")
    if "type" not in payload:
        raise _malformed("$.type", "known string domain type tag")
    raw_tag = payload["type"]
    if type(raw_tag) is not str:
        raise _malformed("$.type", "known string domain type tag")
    if raw_tag not in _TYPE_TO_TAG.values() or raw_tag != expected_tag:
        raise UnknownDomainType("$.type: expected the tag for the requested domain type")
    _require_exact_keys(payload, {"schema_version", "type", "data"}, "$")
    data = payload["data"]

    if expected_type in _ENUM_TYPES or expected_type in _SCALAR_TYPES:
        return _construct_string_value(expected_type, data, "$.data")
    if expected_type is SnapshotRef:
        return cast(T, _decode_snapshot(data, "$.data"))
    if expected_type is FilePair:
        return cast(T, _decode_file_pair(data, "$.data"))
    if expected_type is Diagnostic:
        return cast(T, _decode_diagnostic(data, "$.data"))
    if expected_type is JobResult:
        return cast(T, _decode_job_result(data, "$.data"))
    raise UnknownDomainType("$: expected a supported exact domain type")
