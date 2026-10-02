"""Strict whole-PR critic and independent arbiter boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from importlib import resources
from typing import cast

from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import ModelRequest
from ydbdoc_review_ng.models.types import FrozenJson
from ydbdoc_review_ng.quality.types import CriticResult, Finding, Verdict


class CriticResponseErrorReason(str, Enum):
    MALFORMED_JSON = "malformed_json"
    ROOT_NOT_OBJECT = "root_not_object"
    DUPLICATE_KEY = "duplicate_key"
    UNEXPECTED_FIELD = "unexpected_field"
    INVALID_VERDICT = "invalid_verdict"
    INVALID_FINDING = "invalid_finding"
    INVALID_FILES = "invalid_files"
    INCONSISTENT_RESULT = "inconsistent_result"


class CriticResponseError(ValueError):
    def __init__(self, reason: CriticResponseErrorReason, /) -> None:
        self.reason = reason
        super().__init__(f"critic_response:{reason.value}")


class _ObjectPairs(list[tuple[object, object]]):
    pass


_CRITIC_CHECKLIST = (
    "Before answering, check completeness, terminology, technical literals and "
    "inline-code, damaged sentences, TOC correctness, and the complete requested "
    "file set."
)
_ARBITER_CHECKLIST = (
    "Before answering, check completeness, terminology, technical literals and "
    "inline-code, damaged sentences, TOC correctness, and every supplied file."
)


def _has_duplicate(value: object) -> bool:
    if type(value) is _ObjectPairs:
        pairs = value
        keys = [key for key, _item in pairs]
        return len(keys) != len(set(keys)) or any(_has_duplicate(item) for _key, item in pairs)
    if type(value) is list:
        return any(_has_duplicate(item) for item in cast(list[object], value))
    return False


def _object(
    value: object, expected: frozenset[str], optional: frozenset[str] = frozenset()
) -> dict[str, object]:
    if type(value) is not _ObjectPairs:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
    pairs = value
    if any(type(key) is not str for key, _item in pairs):
        raise CriticResponseError(CriticResponseErrorReason.UNEXPECTED_FIELD)
    result = {cast(str, key): item for key, item in pairs}
    if set(result) - expected - optional or not expected.issubset(result):
        raise CriticResponseError(CriticResponseErrorReason.UNEXPECTED_FIELD)
    return result


def _review_user_prompt(
    *,
    source_files: Mapping[str, bytes],
    translated_files: Mapping[str, bytes | None],
    glossary_files: Mapping[str, bytes],
    toc_snapshots: Mapping[str, Mapping[str, str | None]] | None,
    binary_manifest: Mapping[str, Mapping[str, str]] | None,
    operator_context: str | None,
    checklist: str,
) -> str:
    values: tuple[tuple[str, Mapping[str, object]], ...] = (
        ("source-pr-files", source_files),
        ("translation-pr-files", translated_files),
        ("project-glossary", glossary_files),
        ("source-toc-snapshots", {} if toc_snapshots is None else toc_snapshots),
        ("binary-manifest", {} if binary_manifest is None else binary_manifest),
    )
    blocks: list[str] = []
    for tag, files in values:
        rendered = {
            path: content.decode("utf-8") if isinstance(content, bytes) else content
            for path, content in files.items()
        }
        blocks.append(
            f"<{tag}>\n{json.dumps(rendered, ensure_ascii=False)}\n</{tag}>"
        )
    if operator_context is not None:
        blocks.append(f"<operator-context>\n{operator_context}</operator-context>")
    blocks.append(checklist)
    return "\n\n".join(blocks)


def build_pr_critic_request(
    *,
    model: str,
    source_files: Mapping[str, bytes],
    translated_files: Mapping[str, bytes | None],
    glossary_files: Mapping[str, bytes],
    operator_context: str | None = None,
    toc_snapshots: Mapping[str, Mapping[str, str | None]] | None = None,
    binary_manifest: Mapping[str, Mapping[str, str]] | None = None,
) -> ModelRequest:
    template = (
        resources.files("ydbdoc_review_ng.quality")
        .joinpath("prompts/critic.txt")
        .read_text(encoding="utf-8")
    )
    prompt = _review_user_prompt(
        source_files=source_files,
        translated_files=translated_files,
        glossary_files=glossary_files,
        toc_snapshots=toc_snapshots,
        binary_manifest=binary_manifest,
        operator_context=operator_context,
        checklist=_CRITIC_CHECKLIST,
    )
    schema = {
        "type": "object",
        "properties": {
            "files": {
                "type": "object",
                "properties": {path: {"type": "string"} for path in translated_files},
                "required": list(translated_files),
                "additionalProperties": False,
            }
        },
        "required": ["files"],
        "additionalProperties": False,
    }
    return ModelRequest(
        ModelRole.CRITIC,
        model,
        prompt,
        cast(FrozenJson, schema),
        developer_prompt=template,
    )


def parse_pr_critic_response(
    raw: str | bytes,
    *,
    target_paths: tuple[str, ...],
) -> dict[str, bytes]:
    if type(raw) not in {str, bytes}:
        raise TypeError("raw must be exact str or bytes")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        value = json.loads(text, object_pairs_hook=_ObjectPairs)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise CriticResponseError(CriticResponseErrorReason.MALFORMED_JSON) from None
    if type(value) is not _ObjectPairs:
        raise CriticResponseError(CriticResponseErrorReason.ROOT_NOT_OBJECT)
    if _has_duplicate(value):
        raise CriticResponseError(CriticResponseErrorReason.DUPLICATE_KEY)
    document = _object(value, frozenset({"files"}))
    if type(document["files"]) is not _ObjectPairs:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FILES)
    files = _object(document["files"], frozenset(target_paths))
    if any(type(content) is not str for content in files.values()):
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FILES)
    try:
        return {path: cast(str, content).encode("utf-8") for path, content in files.items()}
    except UnicodeEncodeError:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FILES) from None


def build_pr_arbiter_request(
    *,
    model: str,
    source_files: Mapping[str, bytes],
    translated_files: Mapping[str, bytes | None],
    glossary_files: Mapping[str, bytes],
    operator_context: str | None = None,
    toc_snapshots: Mapping[str, Mapping[str, str | None]] | None = None,
    binary_manifest: Mapping[str, Mapping[str, str]] | None = None,
) -> ModelRequest:
    template = (
        resources.files("ydbdoc_review_ng.quality")
        .joinpath("prompts/arbiter.txt")
        .read_text(encoding="utf-8")
    )
    prompt = _review_user_prompt(
        source_files=source_files,
        translated_files=translated_files,
        glossary_files=glossary_files,
        toc_snapshots=toc_snapshots,
        binary_manifest=binary_manifest,
        operator_context=operator_context,
        checklist=_ARBITER_CHECKLIST,
    )
    # §4.1/§4.2: resource-only scope still needs reportable finding paths.
    allowed_paths = list(translated_files)
    if binary_manifest:
        for path in binary_manifest:
            if path not in allowed_paths:
                allowed_paths.append(path)
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["GREEN", "YELLOW", "RED"]},
            "findings": {
                "type": "array",
                "items": _finding_schema({"type": "string", "enum": allowed_paths}),
            },
        },
        "required": ["verdict", "findings"],
        "additionalProperties": False,
    }
    return ModelRequest(
        ModelRole.ARBITER,
        model,
        prompt,
        cast(FrozenJson, schema),
        developer_prompt=template,
    )


def parse_pr_arbiter_response(
    raw: str | bytes,
    *,
    target_files: Mapping[str, bytes | None],
) -> CriticResult:
    if type(raw) not in {str, bytes}:
        raise TypeError("raw must be exact str or bytes")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        text.encode("utf-8")
        value = json.loads(text, object_pairs_hook=_ObjectPairs)
    except (json.JSONDecodeError, UnicodeError):
        raise CriticResponseError(CriticResponseErrorReason.MALFORMED_JSON) from None
    try:
        target_texts = {
            path: content.decode("utf-8") if content is not None else None
            for path, content in target_files.items()
        }
    except UnicodeDecodeError:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FILES) from None
    if type(value) is not _ObjectPairs:
        raise CriticResponseError(CriticResponseErrorReason.ROOT_NOT_OBJECT)
    if _has_duplicate(value):
        raise CriticResponseError(CriticResponseErrorReason.DUPLICATE_KEY)
    document = _object(value, frozenset({"verdict", "findings"}))
    raw_verdict = document["verdict"]
    if type(raw_verdict) is not str or raw_verdict not in {"GREEN", "YELLOW", "RED"}:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_VERDICT)
    raw_findings = document["findings"]
    findings, has_unresolved_location = _parse_findings(raw_findings, target_texts)
    if (raw_verdict == "GREEN") != (type(raw_findings) is list and not raw_findings):
        raise CriticResponseError(CriticResponseErrorReason.INCONSISTENT_RESULT)
    verdict = Verdict.RED if has_unresolved_location else Verdict(raw_verdict)
    return CriticResult(verdict, findings)


def _finding_schema(target_path_schema: dict[str, object]) -> dict[str, object]:
    finding_properties: dict[str, object] = {
        "reason": {"type": "string", "minLength": 1},
        "expected_correction": {"type": "string", "minLength": 1},
        "searchable_snippet": {"type": ["string", "null"], "minLength": 1},
        "target_path": target_path_schema,
    }
    return {
        "type": "object",
        "properties": finding_properties,
        "required": list(finding_properties),
        "additionalProperties": False,
    }


def _parse_findings(
    raw_findings: object,
    target_texts: Mapping[str, str | None],
) -> tuple[tuple[Finding, ...], bool]:
    if type(raw_findings) is not list:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
    findings: list[Finding] = []
    has_unresolved_location = False
    required = frozenset(
        {"reason", "expected_correction", "searchable_snippet", "target_path"}
    )
    for raw_finding in raw_findings:
        item = _object(raw_finding, required)
        if any(
            type(item[name]) is not str or not cast(str, item[name]).strip()
            for name in ("reason", "expected_correction")
        ):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        try:
            for field_value in item.values():
                if isinstance(field_value, str):
                    field_value.encode("utf-8")
        except UnicodeEncodeError:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING) from None
        finding_path = item["target_path"]
        if type(finding_path) is not str or finding_path not in target_texts:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        target_text = target_texts[finding_path]
        snippet = item["searchable_snippet"]
        if target_text is None:
            if snippet is not None:
                raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
            target_line = None
        elif type(snippet) is not str or not snippet:
            has_unresolved_location = True
            findings.append(_unresolved_arbiter_location(finding_path))
            continue
        else:
            first = target_text.find(snippet)
            if first < 0 or target_text.find(snippet, first + 1) >= 0:
                has_unresolved_location = True
                findings.append(_unresolved_arbiter_location(finding_path))
                continue
            target_line = target_text.count("\n", 0, first) + 1
        findings.append(
            Finding(
                False,
                cast(str, item["reason"]),
                cast(str, item["expected_correction"]),
                snippet,
                finding_path,
                target_line,
            )
        )
    return tuple(findings), has_unresolved_location


def _unresolved_arbiter_location(target_path: str) -> Finding:
    return Finding(
        False,
        "Арбитр не привязал замечание к единственному фрагменту итогового файла.",
        "Повторите проверку файла.",
        None,
        target_path,
        None,
    )
