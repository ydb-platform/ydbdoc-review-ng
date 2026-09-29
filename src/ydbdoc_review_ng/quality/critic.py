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


def critic_schema(
    target_path: RepoPath,
    requested_ids: tuple[str, ...],
    /,
    *,
    editable: bool = False,
) -> dict[str, object]:
    if editable:
        editable_finding = {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "minLength": 1},
                "expected_correction": {"type": "string", "minLength": 1},
                "searchable_snippet": {"type": "string", "minLength": 1},
            },
            "required": ["reason", "expected_correction", "searchable_snippet"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": editable_finding,
                    "maxItems": 20,
                },
                "corrected_markdown": {"type": "string"},
            },
            "required": ["findings", "corrected_markdown"],
            "additionalProperties": False,
        }
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
    properties: dict[str, object] = {
        "verdict": {"type": "string", "enum": ["GREEN", "RED"]},
        "findings": {"type": "array", "items": finding},
    }
    required = ["verdict", "findings"]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
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
    source_is_excerpt: bool = False,
    target_is_excerpt: bool = False,
    operator_context: str | None = None,
    editable: bool = False,
    terminology_context: str | None = None,
    protected_fragments: tuple[tuple[str, str], ...] = (),
) -> ModelRequest:
    if type(source) is not bytes or type(target) is not bytes:
        raise TypeError("source and target must be exact bytes")
    source_text = source.decode("utf-8")
    target_text = target.decode("utf-8")
    if editable:
        field_ids_instruction = (
            "Do not include field_ids; this editor returns the complete corrected excerpt.\n"
        )
    elif requested_ids:
        field_ids_instruction = (
            "Always include field_ids in every finding. Use [] when no safe exact field mapping "
            "exists.\n"
        )
    else:
        field_ids_instruction = "Do not include field_ids.\n"
    if target_is_excerpt:
        source_scope_instruction = (
            "The authoritative source and target below are corresponding ordered excerpts of "
            "larger documents. Review only this excerpt pair. "
        )
    elif source_is_excerpt:
        source_scope_instruction = (
            "The authoritative source below is one ordered excerpt of a larger document. "
            "Review only target material corresponding to this source excerpt. The complete "
            "target is included for context; do not flag its other sections as extra content. "
        )
    else:
        source_scope_instruction = ""
    verdict_instruction = (
        "Act as a critic-editor. Return findings and corrected_markdown only; do not return "
        "verdict, repairable, "
        "target_path, target_line, or field_ids. Keep findings concise and consolidate related "
        "defects. If there are no material defects, return an empty findings array and copy the "
        "current target byte-for-byte into corrected_markdown. If there are defects, report each "
        "one and fix all of them in the same complete corrected_markdown. Preserve every "
        "protected placeholder exactly once and do not add, remove, rename, or reorder "
        "placeholders. "
        if editable
        else "Use GREEN when there are no material defects and RED otherwise. "
    )
    defect_instruction = (
        "Add a finding only for a concrete, currently present, material translation defect: "
        if editable
        else "Use RED only for a concrete, currently present, material translation defect: "
    )
    style_instruction = (
        "Do not add findings for optional stylistic polishing, smoother grammar, tone "
        if editable
        else "Do not return RED for optional stylistic polishing, smoother grammar, tone "
    )
    complete_instruction = (
        "and understandable, do not add findings even if its prose could be polished. "
        if editable
        else "and understandable, return GREEN even if its prose could be polished. "
    )
    field_list_instruction = "" if editable else "List each field ID at most once. "
    prompt = (
        "Compare the authoritative source with the complete translated target. "
        f"{source_scope_instruction}"
        f"{verdict_instruction}{defect_instruction}"
        "wrong or reversed meaning; missing user-facing information; untranslated user-facing "
        "prose; wrong technical terminology that can mislead use; or broken or purpose-changing "
        f"link usage. {style_instruction}"
        "preferences, requests for more detail than the authoritative source, or vague requests "
        'such as "review", "refine", or "could be clearer". If the target is complete, accurate, '
        f"{complete_instruction}Every finding "
        "must name an actual source/target mismatch visible in the current final target, include "
        "an exact searchable snippet copied from the current target, and give a concrete "
        "replacement or correction. Do not report a stale defect that the current target bytes "
        "no longer contain. Operator context is guidance for interpreting intent only; it must "
        "not override the authoritative source or current target bytes and must not force a "
        "finding that is no longer present. "
        "Check full meaning and accuracy, completeness, terminology, untranslated user-facing "
        "prose, and the purpose and workability of links in context. Do not rewrite URLs or "
        "paths, and do not implement or request a navigation resolver. Return only the strict "
        "JSON result. In glossary alias lists, a source-language term and an already supplied "
        "target-language alias may translate to the same target term: keep that term once, do "
        "not require one target occurrence per source alias, and treat a repeated identical "
        "target alias as a defect. Never claim that an alias is missing when its exact term is "
        "already present in the current target. "
        "Write reason and expected_correction in Russian so the public PR comment "
        "is immediately understandable to the documentation author. "
        f"{field_list_instruction}"
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
    if protected_fragments:
        prompt += (
            "\n<protected-fragments>\n"
            + json.dumps(dict(protected_fragments), ensure_ascii=False)
            + "\n</protected-fragments>\n"
            + "The mapping restores the exact code, commands, links, and identifiers behind "
            + "every placeholder. Use it while reviewing meaning, but preserve the placeholder "
            + "tokens unchanged in corrected_markdown."
        )
    if operator_context is not None:
        prompt += "\n<operator-context>\n" + operator_context + "</operator-context>"
    if terminology_context:
        prompt += (
            "\n<project-glossary>\n"
            + terminology_context
            + "\n</project-glossary>\nUse these target-language terms when judging or correcting."
        )
    role = ModelRole.ARBITER if final else ModelRole.CRITIC
    schema = cast(FrozenJson, critic_schema(target_path, requested_ids, editable=editable))
    return ModelRequest(
        role,
        model,
        prompt,
        schema,
        8000 if editable else 2000,
        target_path,
    )


def parse_critic_response(
    raw: str | bytes,
    *,
    target_path: RepoPath,
    requested_ids: tuple[str, ...],
    editable: bool = False,
    current_target: str | None = None,
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
    expected = frozenset(
        {"findings", "corrected_markdown"} if editable else {"verdict", "findings"}
    )
    optional_root = frozenset({"verdict"}) if editable else frozenset()
    document = _object(value, expected, optional_root)
    raw_verdict = document.get("verdict")
    if raw_verdict is not None and (
        type(raw_verdict) is not str or raw_verdict not in {"GREEN", "RED"}
    ):
        raise CriticResponseError(CriticResponseErrorReason.INVALID_VERDICT)
    raw_findings = document["findings"]
    if type(raw_findings) is not list:
        raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
    findings: list[Finding] = []
    base_required = frozenset({"reason", "expected_correction", "searchable_snippet"})
    legacy_required = frozenset({"repairable", "target_path", "target_line"})
    required = base_required if editable else base_required | legacy_required
    allowed_ids = set(requested_ids)
    for raw_finding in raw_findings:
        if type(raw_finding) is not _ObjectPairs:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        finding_keys = {key for key, _item in raw_finding if type(key) is str}
        if not required.issubset(finding_keys):
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        try:
            optional = (
                frozenset({"repairable", "target_path", "target_line", "field_ids"})
                if editable
                else (frozenset({"field_ids"}) if requested_ids else frozenset())
            )
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
        finding_path = item.get("target_path", target_path.value)
        if type(finding_path) is not str or finding_path != target_path.value:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        repairable = item.get("repairable", True)
        if type(repairable) is not bool:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        snippet = cast(str, item["searchable_snippet"])
        snippet_offset = current_target.find(snippet) if current_target is not None else -1
        derived_line = (
            current_target.count("\n", 0, snippet_offset) + 1
            if current_target is not None and snippet_offset >= 0
            else 1
        )
        target_line = item.get("target_line", derived_line)
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
    # Findings are the canonical verdict signal. Provider-side JSON schema can
    # validate both fields independently but cannot reliably enforce their
    # cross-field relationship across supported model backends. Normalizing the
    # redundant verdict avoids rejecting an otherwise complete strict response.
    verdict = Verdict.RED if findings else Verdict.GREEN
    corrected_markdown: str | None = None
    if editable:
        raw_correction = document["corrected_markdown"]
        if type(raw_correction) is not str or current_target is None:
            raise CriticResponseError(CriticResponseErrorReason.INVALID_FINDING)
        corrected_markdown = raw_correction
    return CriticResult(verdict, tuple(findings), corrected_markdown)
