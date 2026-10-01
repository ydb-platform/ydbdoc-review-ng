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
    translated_files: Mapping[str, bytes],
    glossary_files: Mapping[str, bytes],
    operator_context: str | None = None,
) -> ModelRequest:
    prompt = (
        "You are an independent, read-only arbiter of a YDB documentation translation. "
        "Compare the complete current source PR files with the complete final translated PR "
        "files as one pull request, using the complete project glossary. Check completeness, "
        "accuracy, consistent terminology across files, glossary compliance, technical literals, "
        "untranslated prose, Markdown/YFM readability, links and navigation consistency. "
        "Return GREEN for a correct translation, YELLOW for remaining lesser problems, "
        "or RED for serious translation problems. Judge the degree of problems, not their count. "
        "Return only the strict JSON verdict and findings. Do not return corrected files, "
        "patches or corrected_markdown. Findings go directly to the public report and are "
        "not instructions for another model or an automatic repair loop. "
        "For every finding, give the translated target_path, exact target_line, an exact "
        "searchable_snippet from the current final translation, a concrete reason and "
        "expected_correction. Write reason and expected_correction in Russian. "
        "The repairable field is compatibility metadata only; it does not request a repair. "
        "Do not include field_ids. Operator context is guidance only and must not override "
        "the source or final translation or force a finding no longer present.\n"
    )
    for tag, files in (
        ("source-pr-files", source_files),
        ("translation-pr-files", translated_files),
        ("project-glossary", glossary_files),
    ):
        content = json.dumps(
            {path: content.decode("utf-8") for path, content in files.items()},
            ensure_ascii=False,
        )
        prompt += f"<{tag}>\n{content}\n</{tag}>\n"
    if operator_context is not None:
        prompt += "\n<operator-context>\n" + operator_context + "</operator-context>"
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["GREEN", "YELLOW", "RED"]},
            "findings": {
                "type": "array",
                "items": _finding_schema({"type": "string", "enum": list(translated_files)}, ()),
            },
        },
        "required": ["verdict", "findings"],
        "additionalProperties": False,
    }
    return ModelRequest(ModelRole.ARBITER, model, prompt, cast(FrozenJson, schema))


def parse_pr_arbiter_response(
    raw: str | bytes,
    *,
    target_paths: tuple[str, ...],
) -> CriticResult:
    if type(raw) not in {str, bytes}:
        raise TypeError("raw must be exact str or bytes")
    try:
        value = json.loads(raw, object_pairs_hook=_ObjectPairs)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise CriticResponseError(CriticResponseErrorReason.MALFORMED_JSON) from None
    if type(value) is not _ObjectPairs:
        raise CriticResponseError(CriticResponseErrorReason.ROOT_NOT_OBJECT)
    if _has_duplicate(value):
        raise CriticResponseError(CriticResponseErrorReason.DUPLICATE_KEY)
    document = _object(value, frozenset({"verdict", "findings"}))
    raw_verdict = document["verdict"]
    if type(raw_verdict) is not str or raw_verdict not in {"GREEN", "YELLOW", "RED"}:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_VERDICT)
    findings = _parse_findings(document["findings"], target_paths, ())
    return CriticResult(Verdict(raw_verdict), findings)


def _finding_schema(
    target_path_schema: dict[str, object],
    requested_ids: tuple[str, ...],
) -> dict[str, object]:
    finding_properties: dict[str, object] = {
        "repairable": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1},
        "expected_correction": {"type": "string", "minLength": 1},
        "searchable_snippet": {"type": "string", "minLength": 1},
        "target_path": target_path_schema,
        "target_line": {"type": "integer", "minimum": 1},
    }
    if requested_ids:
        finding_properties["field_ids"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(requested_ids)},
            "uniqueItems": True,
        }
    return {
        "type": "object",
        "properties": finding_properties,
        "required": list(finding_properties),
        "additionalProperties": False,
    }


def _parse_findings(
    raw_findings: object,
    target_paths: tuple[str, ...],
    requested_ids: tuple[str, ...],
) -> tuple[Finding, ...]:
    if type(raw_findings) is not list:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
    findings: list[Finding] = []
    base_required = frozenset({"reason", "expected_correction", "searchable_snippet"})
    legacy_required = frozenset({"repairable", "target_path", "target_line"})
    required = base_required | legacy_required
    allowed_ids = set(requested_ids)
    for raw_finding in raw_findings:
        if type(raw_finding) is not _ObjectPairs:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        finding_keys = {key for key, _item in raw_finding if type(key) is str}
        if not required.issubset(finding_keys):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        try:
            optional = frozenset({"field_ids"}) if requested_ids else frozenset()
            item = _object(raw_finding, required, optional)
        except CriticResponseError as error:
            if error.reason is CriticResponseErrorReason.UNEXPECTED_FIELD:
                raise
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING) from None
        string_names = ("reason", "expected_correction", "searchable_snippet")
        if any(
            type(item[name]) is not str or not cast(str, item[name]).strip()
            for name in string_names
        ):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        finding_path = item["target_path"]
        if type(finding_path) is not str or finding_path not in target_paths:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        repairable = item.get("repairable", True)
        if type(repairable) is not bool:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        target_line = item["target_line"]
        if type(target_line) is not int or target_line < 1:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        raw_ids = item.get("field_ids", [])
        if type(raw_ids) is not list or any(type(field_id) is not str for field_id in raw_ids):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        field_ids = list(dict.fromkeys(cast(list[str], raw_ids)))
        if any(value not in allowed_ids for value in field_ids):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        if not repairable and field_ids:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        findings.append(
            Finding(
                repairable,
                cast(str, item["reason"]),
                cast(str, item["expected_correction"]),
                cast(str, item["searchable_snippet"]),
                finding_path,
                target_line,
                tuple(field_ids),
            )
        )
    return tuple(findings)
