"""Strict whole-document source/target critic boundary."""

from __future__ import annotations

import json
from enum import Enum
from typing import cast

from ydbdoc_review_ng.domain import Locale, ModelRole, RepoPath
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


def critic_schema(target_path: RepoPath, requested_ids: tuple[str, ...], /) -> dict[str, object]:
    finding_properties: dict[str, object] = {
        "repairable": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1},
        "expected_correction": {"type": "string", "minLength": 1},
        "searchable_snippet": {"type": "string", "minLength": 1},
        "target_path": {"type": "string", "const": target_path.value},
        "target_line": {"type": "integer", "minimum": 1},
    }
    if requested_ids:
        finding_properties["field_ids"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(requested_ids)},
            "uniqueItems": True,
        }
    finding: dict[str, object] = {
        "type": "object",
        "properties": finding_properties,
        "required": list(finding_properties),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["GREEN", "RED"]},
            "findings": {"type": "array", "items": finding},
        },
        "required": ["verdict", "findings"],
        "additionalProperties": False,
    }


def build_critic_request(
    *,
    model: str,
    source: bytes,
    target: bytes,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    requested_ids: tuple[str, ...],
    final: bool = False,
    operator_context: str | None = None,
) -> ModelRequest:
    if type(source) is not bytes or type(target) is not bytes:
        raise TypeError("source and target must be exact bytes")
    source_text = source.decode("utf-8")
    target_text = target.decode("utf-8")
    field_ids_instruction = (
        "Always include field_ids in every finding. Use [] when no safe exact field mapping "
        "exists.\n"
        if requested_ids
        else "Do not include field_ids because this document has no repairable fields.\n"
    )
    prompt = (
        "Compare the authoritative source with the complete translated target. "
        "Use RED only for a concrete, currently present, material translation defect: "
        "wrong or reversed meaning; missing user-facing information; untranslated user-facing "
        "prose; wrong technical terminology that can mislead use; or broken or purpose-changing "
        "link usage. Do not return RED for optional stylistic polishing, smoother grammar, tone "
        "preferences, requests for more detail than the authoritative source, or vague requests "
        'such as "review", "refine", or "could be clearer". If the target is complete, accurate, '
        "and understandable, return GREEN even if its prose could be polished. Every RED finding "
        "must name an actual source/target mismatch visible in the current final target, include "
        "an exact searchable snippet copied from the current target, and give a concrete "
        "replacement or correction. Do not report a stale defect that the current target bytes "
        "no longer contain. Operator context is guidance for interpreting intent only; it must "
        "not override the authoritative source or current target bytes and must not force a "
        "finding that is no longer present. "
        "Check full meaning and accuracy, completeness, terminology, untranslated user-facing "
        "prose, and the purpose and workability of links in context. Do not rewrite URLs or "
        "paths, and do not implement or request a navigation resolver. Return only the strict "
        "JSON result. "
        "List each field ID at most once. "
        f"{field_ids_instruction}"
        f"Direction: {source_locale.value} -> {target_locale.value}\n"
        f"Target path: {target_path.value}\n"
        "<authoritative-source>\n"
        f"{source_text}"
        "</authoritative-source>\n"
        "<final-target>\n"
        f"{target_text}"
        "</final-target>"
    )
    if operator_context is not None:
        prompt += "\n<operator-context>\n" + operator_context + "</operator-context>"
    role = ModelRole.FINAL_CRITIC if final else ModelRole.CRITIC
    schema = cast(FrozenJson, critic_schema(target_path, requested_ids))
    return ModelRequest(role, model, prompt, schema, target_path=target_path)


def parse_critic_response(
    raw: str | bytes,
    *,
    target_path: RepoPath,
    requested_ids: tuple[str, ...],
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
    if type(raw_verdict) is not str or raw_verdict not in {"GREEN", "RED"}:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_VERDICT)
    raw_findings = document["findings"]
    if type(raw_findings) is not list:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
    findings: list[Finding] = []
    required = frozenset(
        {
            "repairable",
            "reason",
            "expected_correction",
            "searchable_snippet",
            "target_path",
            "target_line",
        }
    )
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
        string_names = ("reason", "expected_correction", "searchable_snippet", "target_path")
        if any(
            type(item[name]) is not str or not cast(str, item[name]).strip()
            for name in string_names
        ):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        if item["target_path"] != target_path.value:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        if type(item["repairable"]) is not bool:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        if type(item["target_line"]) is not int or item["target_line"] < 1:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        raw_ids = item.get("field_ids", [])
        if type(raw_ids) is not list or any(type(field_id) is not str for field_id in raw_ids):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        field_ids = list(dict.fromkeys(cast(list[str], raw_ids)))
        if any(value not in allowed_ids for value in field_ids):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        if not item["repairable"] and field_ids:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        findings.append(
            Finding(
                item["repairable"],
                cast(str, item["reason"]),
                cast(str, item["expected_correction"]),
                cast(str, item["searchable_snippet"]),
                item["target_path"],
                item["target_line"],
                tuple(field_ids),
            )
        )
    verdict = Verdict(raw_verdict)
    if (verdict is Verdict.GREEN and findings) or (verdict is Verdict.RED and not findings):
        raise CriticResponseError(CriticResponseErrorReason.INCONSISTENT_RESULT)
    return CriticResult(verdict, tuple(findings))
