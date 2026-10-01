"""Strict whole-PR critic and independent arbiter boundaries."""

from __future__ import annotations

import json
import re
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


def build_pr_critic_request(
    *,
    model: str,
    source_files: Mapping[str, bytes],
    translated_files: Mapping[str, bytes],
    glossary_files: Mapping[str, bytes],
    operator_context: str | None = None,
) -> ModelRequest:
    template = (
        resources.files("ydbdoc_review_ng.quality")
        .joinpath("prompts/critic.txt")
        .read_text(encoding="utf-8")
    )
    values = {
        "SOURCE_PR_FILES": source_files,
        "TRANSLATION_PR_FILES": translated_files,
        "PROJECT_GLOSSARY": glossary_files,
    }
    rendered = {
        name: json.dumps(
            {path: content.decode("utf-8") for path, content in files.items()},
            ensure_ascii=False,
        )
        for name, files in values.items()
    }
    prompt = re.sub(
        r"\{\{ (SOURCE_PR_FILES|TRANSLATION_PR_FILES|PROJECT_GLOSSARY) \}\}",
        lambda match: rendered[match.group(1)],
        template,
    )
    if operator_context is not None:
        prompt += "\n<operator-context>\n" + operator_context + "</operator-context>"
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
    return ModelRequest(ModelRole.CRITIC, model, prompt, cast(FrozenJson, schema))


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
) -> ModelRequest:
    template = (
        resources.files("ydbdoc_review_ng.quality")
        .joinpath("prompts/arbiter.txt")
        .read_text(encoding="utf-8")
    )
    values = {
        "SOURCE_PR_FILES": source_files,
        "TRANSLATION_PR_FILES": translated_files,
        "PROJECT_GLOSSARY": glossary_files,
    }
    rendered = {
        name: json.dumps(
            {
                path: content.decode("utf-8") if content is not None else None
                for path, content in files.items()
            },
            ensure_ascii=False,
        )
        for name, files in values.items()
    }
    prompt = re.sub(
        r"\{\{ (SOURCE_PR_FILES|TRANSLATION_PR_FILES|PROJECT_GLOSSARY) \}\}",
        lambda match: rendered[match.group(1)],
        template,
    )
    if operator_context is not None:
        prompt += "\n<operator-context>\n" + operator_context + "</operator-context>"
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["GREEN", "YELLOW", "RED"]},
            "findings": {
                "type": "array",
                "items": _finding_schema({"type": "string", "enum": list(translated_files)}),
            },
        },
        "required": ["verdict", "findings"],
        "additionalProperties": False,
    }
    return ModelRequest(ModelRole.ARBITER, model, prompt, cast(FrozenJson, schema))


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
        target_lines = {
            path: [line.decode("utf-8") for line in content.splitlines()]
            if content is not None
            else None
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
    findings = _parse_findings(document["findings"], target_lines)
    if (raw_verdict == "GREEN") != (not findings):
        raise CriticResponseError(CriticResponseErrorReason.INCONSISTENT_RESULT)
    return CriticResult(Verdict(raw_verdict), findings)


def _finding_schema(target_path_schema: dict[str, object]) -> dict[str, object]:
    finding_properties: dict[str, object] = {
        "reason": {"type": "string", "minLength": 1},
        "expected_correction": {"type": "string", "minLength": 1},
        "searchable_snippet": {"type": ["string", "null"], "minLength": 1},
        "target_path": target_path_schema,
        "target_line": {"type": ["integer", "null"], "minimum": 1},
    }
    return {
        "type": "object",
        "properties": finding_properties,
        "required": list(finding_properties),
        "additionalProperties": False,
    }


def _parse_findings(
    raw_findings: object,
    target_lines: Mapping[str, list[str] | None],
) -> tuple[Finding, ...]:
    if type(raw_findings) is not list:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
    findings: list[Finding] = []
    required = frozenset(
        {"reason", "expected_correction", "searchable_snippet", "target_path", "target_line"}
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
        if type(finding_path) is not str or finding_path not in target_lines:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        lines = target_lines[finding_path]
        target_line = item["target_line"]
        snippet = item["searchable_snippet"]
        if lines is None:
            if target_line is not None or snippet is not None:
                raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        elif (
            type(target_line) is not int
            or target_line < 1
            or target_line > len(lines)
            or type(snippet) is not str
            or not snippet
            or snippet not in lines[target_line - 1]
        ):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        findings.append(
            Finding(
                False,
                cast(str, item["reason"]),
                cast(str, item["expected_correction"]),
                cast(str, snippet),
                finding_path,
                cast(int, target_line),
            )
        )
    return tuple(findings)
