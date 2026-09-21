"""Bounded quality orchestration: one critic, at most one repair, one final critic."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol, cast

from ydbdoc_review_ng.continuation import AcceptedMap
from ydbdoc_review_ng.domain import Locale, ModelRole, RepoPath
from ydbdoc_review_ng.models import ModelCallResult, ModelRequest
from ydbdoc_review_ng.models.types import FrozenJson
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind, SourcePlan, fields_of
from ydbdoc_review_ng.quality.critic import build_critic_request, parse_critic_response
from ydbdoc_review_ng.quality.types import (
    CriticResult,
    Finding,
    QualityReviewResult,
    RepairErrorReason,
)
from ydbdoc_review_ng.translation import (
    AssemblyError,
    ProtectedMismatch,
    ResponseError,
    TranslationRequest,
    assemble_candidate,
    build_translation_request,
    parse_translation_response,
    verify_protected_fragments,
)
from ydbdoc_review_ng.translation.contract import field_request_text


class ModelExecutor(Protocol):
    def invoke(self, request: ModelRequest, /) -> ModelCallResult: ...


class QualityInputError(ValueError):
    def __init__(self) -> None:
        super().__init__("quality_input:inconsistent_candidate")


class QualityExecutionError(RuntimeError):
    def __init__(self, stage: str, /) -> None:
        self.stage = stage
        super().__init__(f"quality_execution:{stage}")


def _derive_target_translations(
    source: bytes,
    source_plan: SourcePlan,
    translation_request: TranslationRequest,
    target: bytes,
    target_path: RepoPath,
) -> dict[str, str]:
    if translation_request != build_translation_request(source, source_plan):
        raise QualityInputError
    target_plan = build_markdown_plan(source_plan.source_snapshot, target_path, target)
    try:
        verify_protected_fragments(source, source_plan, target, target_plan)
    except (ProtectedMismatch, TypeError, ValueError):
        raise QualityInputError from None
    target_fields = fields_of(target_plan)
    if len(target_fields) != len(translation_request.fields):
        raise QualityInputError
    values: dict[str, str] = {}
    for request_field, target_field in zip(translation_request.fields, target_fields, strict=True):
        source_groups: dict[int, tuple[tuple[ProtectedKind, bytes], ...]] = {}
        for placeholder in request_field.placeholders:
            if placeholder.group is not None:
                source_groups.setdefault(placeholder.group, ())
                source_groups[placeholder.group] += ((placeholder.kind, placeholder.source_bytes),)
        target_groups: dict[int, tuple[tuple[ProtectedKind, bytes], ...]] = {}
        for region in target_field.protected_regions:
            if region.group is not None:
                target_groups.setdefault(region.group, ())
                target_groups[region.group] += (
                    (region.kind, target[region.span.start : region.span.end]),
                )
        group_map: dict[int, int] = {}
        unused_source_groups = set(source_groups)
        for target_group, signature in target_groups.items():
            group_match = next(
                (
                    source_group
                    for source_group in unused_source_groups
                    if source_groups[source_group] == signature
                ),
                None,
            )
            if group_match is None:
                raise QualityInputError
            group_map[target_group] = group_match
            unused_source_groups.remove(group_match)

        unused = list(request_field.placeholders)
        chunks: list[bytes] = []
        cursor = target_field.span.start
        for region in target_field.protected_regions:
            chunks.append(target[cursor : region.span.start])
            region_bytes = target[region.span.start : region.span.end]
            expected_group = group_map.get(region.group) if region.group is not None else None
            placeholder_match = next(
                (
                    placeholder
                    for placeholder in unused
                    if placeholder.kind is region.kind
                    and placeholder.source_bytes == region_bytes
                    and placeholder.group == expected_group
                ),
                None,
            )
            if placeholder_match is None:
                raise QualityInputError
            chunks.append(placeholder_match.token.encode("ascii"))
            unused.remove(placeholder_match)
            cursor = region.span.end
        chunks.append(target[cursor : target_field.span.end])
        if unused:
            raise QualityInputError
        try:
            values[request_field.field_id] = field_request_text(
                target, target_plan, target_field, b"".join(chunks)
            )
        except UnicodeDecodeError:
            raise QualityInputError from None
    try:
        if assemble_candidate(source, source_plan, translation_request, values) != target:
            raise QualityInputError
    except AssemblyError:
        raise QualityInputError from None
    return values


def _invoke_critic(
    executor: ModelExecutor,
    *,
    model: str,
    source: bytes,
    target: bytes,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    requested_ids: tuple[str, ...],
    final: bool,
) -> CriticResult:
    request = build_critic_request(
        model=model,
        source=source,
        target=target,
        target_path=target_path,
        source_locale=source_locale,
        target_locale=target_locale,
        requested_ids=requested_ids,
        final=final,
    )
    response = executor.invoke(request)
    if not response.success or response.text is None:
        raise QualityExecutionError("final_critic" if final else "critic")
    return parse_critic_response(
        response.text, target_path=target_path, requested_ids=requested_ids
    )


def _safe_repair_findings(
    findings: tuple[Finding, ...],
    source_plan: SourcePlan,
    target: bytes,
) -> tuple[tuple[Finding, ...], tuple[str, ...]]:
    target_plan = build_markdown_plan(source_plan.source_snapshot, source_plan.source_path, target)
    source_fields = fields_of(source_plan)
    target_fields = fields_of(target_plan)
    positions = {field.field_id.value: position for position, field in enumerate(source_fields)}
    selected_findings: list[Finding] = []
    selected_ids: set[str] = set()
    for finding in findings:
        if not finding.repairable or not finding.field_ids:
            continue
        safe = True
        for field_id in finding.field_ids:
            position = positions.get(field_id)
            if position is None or position >= len(target_fields):
                safe = False
                break
            target_field = target_fields[position]
            field_text = target[target_field.span.start : target_field.span.end].decode("utf-8")
            if (
                not target_field.lines.start <= finding.target_line <= target_field.lines.end
                or finding.searchable_snippet not in field_text
            ):
                safe = False
                break
        if safe:
            selected_findings.append(finding)
            selected_ids.update(finding.field_ids)
    ordered = tuple(
        field.field_id for field in source_fields if field.field_id.value in selected_ids
    )
    return tuple(selected_findings), tuple(item.value for item in ordered)


def _repair_request(
    *,
    model: str,
    source: bytes,
    target: bytes,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    translation_request: TranslationRequest,
    findings: tuple[Finding, ...],
    field_ids: tuple[str, ...],
) -> tuple[ModelRequest, TranslationRequest]:
    allowed = set(field_ids)
    fields = tuple(field for field in translation_request.fields if field.field_id in allowed)
    subset = TranslationRequest(tuple(field.field_id for field in fields), fields)
    problems = [
        {
            "reason": finding.reason,
            "expected_correction": finding.expected_correction,
            "searchable_snippet": finding.searchable_snippet,
            "target_path": finding.target_path,
            "target_line": finding.target_line,
            "field_ids": list(finding.field_ids),
        }
        for finding in findings
    ]
    allowed_fields = [
        {
            "field_id": field.field_id,
            "source_field_text": field.text,
            "placeholders": [item.token for item in field.placeholders],
        }
        for field in fields
    ]
    prompt = (
        "Repair only the allowed translated fields for the listed problems. Return one strict "
        "JSON object mapping every allowed field_id to its complete corrected string. Preserve "
        "each placeholder exactly once. The authoritative source and source-only assembler are "
        "authoritative; never use old target fragments as an assembly template. Do not change "
        "URLs, paths, anchors, code, or placeholders.\n"
        f"Direction: {source_locale.value} -> {target_locale.value}\n"
        f"Target path: {target_path.value}\n"
        "<authoritative-source>\n"
        f"{source.decode('utf-8')}"
        "</authoritative-source>\n"
        "<actual-target-context>\n"
        f"{target.decode('utf-8')}"
        "</actual-target-context>\n"
        f"Problems: {json.dumps(problems, ensure_ascii=False)}\n"
        f"Allowed fields: {json.dumps(allowed_fields, ensure_ascii=False)}"
    )
    properties = {field_id: {"type": "string"} for field_id in subset.requested_ids}
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(subset.requested_ids),
        "additionalProperties": False,
    }
    return ModelRequest(ModelRole.REPAIR, model, prompt, cast(FrozenJson, schema)), subset


def review_translation(
    executor: ModelExecutor,
    *,
    model: str,
    source: bytes,
    source_plan: SourcePlan,
    translation_request: TranslationRequest,
    target: bytes,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    before_final_critic: Callable[[bytes], None] | None = None,
    allow_repair: bool = True,
    accepted_map: AcceptedMap | None = None,
) -> QualityReviewResult:
    """Review the actual candidate and apply no more than one source-only repair."""
    if accepted_map is None:
        target_translations = _derive_target_translations(
            source, source_plan, translation_request, target, target_path
        )
    else:
        target_translations = accepted_map.as_dict()
        if (
            accepted_map.target_path != target_path
            or assemble_candidate(source, source_plan, translation_request, target_translations)
            != target
        ):
            raise QualityInputError
    primary = _invoke_critic(
        executor,
        model=model,
        source=source,
        target=target,
        target_path=target_path,
        source_locale=source_locale,
        target_locale=target_locale,
        requested_ids=translation_request.requested_ids,
        final=False,
    )
    repair_findings, repair_ids = _safe_repair_findings(primary.findings, source_plan, target)
    if not repair_ids or not allow_repair:
        return QualityReviewResult(
            target,
            None,
            target,
            primary,
            primary,
            False,
            False,
            None,
            (AcceptedMap(target_path, tuple(sorted(target_translations.items()))),),
        )

    repair_request, subset = _repair_request(
        model=model,
        source=source,
        target=target,
        target_path=target_path,
        source_locale=source_locale,
        target_locale=target_locale,
        translation_request=translation_request,
        findings=repair_findings,
        field_ids=repair_ids,
    )
    repair_response = executor.invoke(repair_request)
    repair_error: RepairErrorReason | None = None
    repaired_candidate: bytes | None = None
    if not repair_response.success or repair_response.text is None:
        raise QualityExecutionError("repair")
    else:
        try:
            repaired_values = parse_translation_response(repair_response.text, subset)
            merged = dict(target_translations)
            merged.update(repaired_values)
            repaired_candidate = assemble_candidate(
                source, source_plan, translation_request, merged
            )
            target_translations = merged
        except ResponseError:
            repair_error = RepairErrorReason.INVALID_RESPONSE
        except (AssemblyError, UnicodeError):
            repair_error = RepairErrorReason.ASSEMBLY_FAILED
    final_candidate = repaired_candidate if repaired_candidate is not None else target
    if repaired_candidate is not None and before_final_critic is not None:
        before_final_critic(repaired_candidate)
    final = _invoke_critic(
        executor,
        model=model,
        source=source,
        target=final_candidate,
        target_path=target_path,
        source_locale=source_locale,
        target_locale=target_locale,
        requested_ids=translation_request.requested_ids,
        final=True,
    )
    return QualityReviewResult(
        target,
        repaired_candidate,
        final_candidate,
        primary,
        final,
        True,
        repaired_candidate is not None,
        repair_error,
        (AcceptedMap(target_path, tuple(sorted(target_translations.items()))),),
    )
