"""Bounded quality orchestration: one critic, at most one repair, one final critic."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from ydbdoc_review_ng.continuation import AcceptedMap
from ydbdoc_review_ng.domain import Locale, ModelRole, RepoPath
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
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
    DocumentChunk,
    DocumentTranslationError,
    DocumentTranslationRequest,
    ProtectedMismatch,
    TranslationRequest,
    build_translation_request,
    prepare_document,
    restore_document,
    split_content_filter_chunk,
    validate_chunk_response,
    verify_document_candidate,
)
from ydbdoc_review_ng.translation.contract import field_request_text
from ydbdoc_review_ng.translation.document import (
    RAW_MARKDOWN_RESPONSE_MAX_CHARACTERS,
    _document_block_texts,
)


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
        verify_document_candidate(source, source_plan, target, target_plan)
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
            if group_match is not None:
                group_map[target_group] = group_match
                unused_source_groups.remove(group_match)

        unused = list(request_field.placeholders)
        chunks: list[bytes] = []
        cursor = target_field.span.start
        for region in target_field.protected_regions:
            chunks.append(target[cursor : region.span.start])
            region_bytes = target[region.span.start : region.span.end]
            expected_group = group_map.get(region.group) if region.group is not None else None
            if region.group is not None and expected_group is None:
                chunks.append(region_bytes)
                cursor = region.span.end
                continue
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
                chunks.append(region_bytes)
                cursor = region.span.end
                continue
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
    operator_context: str | None = None,
    before_model_call: Callable[[], None] | None = None,
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
        operator_context=operator_context,
    )
    if before_model_call is not None:
        before_model_call()
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


def _repair_prompt(
    *,
    source_text: str,
    target_text: str,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    findings: tuple[Finding, ...],
    operator_context: str | None = None,
) -> str:
    problems = [
        {
            "reason": finding.reason,
            "expected_correction": finding.expected_correction,
            "searchable_snippet": finding.searchable_snippet,
            "target_path": finding.target_path,
            "target_line": finding.target_line,
        }
        for finding in findings
    ]
    prompt = (
        "Repair the complete translated Markdown for the listed problems. Return Markdown only, "
        "without JSON, explanations, or an outer code fence. Preserve Markdown/YFM structure "
        "with each placeholder exactly once in its source top-level block. Independent "
        "inline-code/template may move in-field for grammar; all others retain source "
        "order/pairing. "
        "The source is authoritative for protected bytes; the current target is linguistic "
        "context only.\n"
        f"Direction: {source_locale.value} -> {target_locale.value}\n"
        f"Target path: {target_path.value}\n"
        f"Problems: {json.dumps(problems, ensure_ascii=False)}\n"
        "<authoritative-source>\n"
        f"{source_text}"
        "</authoritative-source>\n"
        "<current-target>\n"
        f"{target_text}"
        "</current-target>"
    )
    if operator_context is not None:
        prompt += "\n<operator-context>\n" + operator_context + "</operator-context>"
    return prompt


def _repair_requests(
    *,
    model: str,
    source: bytes,
    source_plan: SourcePlan,
    target: bytes,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    findings: tuple[Finding, ...],
    operator_context: str | None,
    max_characters: int,
) -> tuple[
    tuple[ModelRequest, ...],
    DocumentTranslationRequest,
    tuple[str, ...],
    tuple[str, ...],
]:
    target_plan = build_markdown_plan(source_plan.source_snapshot, target_path, target)
    try:
        verify_document_candidate(source, source_plan, target, target_plan)
    except (ProtectedMismatch, TypeError, ValueError):
        raise QualityInputError from None
    source_document = prepare_document(source, source_plan, max_characters=2**63 - 1)
    target_document = prepare_document(target, target_plan, max_characters=2**63 - 1)
    if len(source_plan.blocks) != len(target_plan.blocks):
        raise QualityInputError
    unused_source = list(source_document.placeholders)
    target_replacements: dict[str, str] = {}
    for target_placeholder in target_document.placeholders:
        source_match = next(
            (
                item
                for item in unused_source
                if item.kind is target_placeholder.kind
                and item.source_bytes == target_placeholder.source_bytes
            ),
            None,
        )
        if source_match is None:
            target_replacements[target_placeholder.token] = target_placeholder.source_bytes.decode(
                "utf-8"
            )
        else:
            target_replacements[target_placeholder.token] = source_match.token
            unused_source.remove(source_match)
    if unused_source:
        raise QualityInputError
    source_blocks = _document_block_texts(source, source_plan, source_document.placeholders)

    def align_target_tokens(block: str) -> str:
        for token, replacement in target_replacements.items():
            block = block.replace(token, replacement)
        return block

    target_blocks = tuple(
        align_target_tokens(block)
        for block in _document_block_texts(target, target_plan, target_document.placeholders)
    )

    def unit(start: int, end: int) -> tuple[DocumentChunk, ModelRequest]:
        source_text = "".join(source_blocks[start:end])
        target_text = "".join(target_blocks[start:end])
        block_start = source_plan.blocks[start].span.start if start < end else 0
        block_end = source_plan.blocks[end - 1].span.end if start < end else 0
        tokens = tuple(
            item.token
            for item in source_document.placeholders
            if block_start <= item.source_start and item.source_end <= block_end
        )
        chunk = DocumentChunk(source_text, start, end, tokens)
        prompt = _repair_prompt(
            source_text=source_text,
            target_text=target_text,
            target_path=target_path,
            source_locale=source_locale,
            target_locale=target_locale,
            findings=findings,
            operator_context=operator_context,
        )
        return chunk, ModelRequest(
            ModelRole.REPAIR, model, prompt, None, 8000, target_path
        )

    if not source_blocks:
        chunk, request = unit(0, 0)
        if len(request.prompt) > max_characters:
            raise QualityInputError
        return (
            (request,),
            DocumentTranslationRequest((chunk,), source_document.placeholders),
            source_blocks,
            target_blocks,
        )
    chunks: list[DocumentChunk] = []
    requests: list[ModelRequest] = []
    start = 0
    while start < len(source_blocks):
        end = start + 1
        accepted: tuple[DocumentChunk, ModelRequest] | None = None
        while end <= len(source_blocks):
            candidate = unit(start, end)
            if (
                max(
                    len("".join(source_blocks[start:end])),
                    len("".join(target_blocks[start:end])),
                )
                > RAW_MARKDOWN_RESPONSE_MAX_CHARACTERS
                or len(candidate[1].prompt) > max_characters
            ):
                break
            accepted = candidate
            end += 1
        if accepted is None:
            raise QualityInputError
        chunk, request = accepted
        chunks.append(chunk)
        requests.append(request)
        start = chunk.block_end
    return (
        tuple(requests),
        DocumentTranslationRequest(tuple(chunks), source_document.placeholders),
        source_blocks,
        target_blocks,
    )


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
    full_repair: bool = False,
    operator_context: str | None = None,
    before_model_call: Callable[[], None] | None = None,
    before_repaired_map: Callable[[AcceptedMap], None] | None = None,
    max_request_characters: int = 200_000,
) -> QualityReviewResult:
    """Review the actual candidate and apply no more than one source-only repair."""
    if accepted_map is None and not full_repair:
        target_translations = _derive_target_translations(
            source, source_plan, translation_request, target, target_path
        )
    elif accepted_map is not None:
        if accepted_map.target_path != target_path:
            raise QualityInputError
        # The checkpoint already binds the accepted map to the immutable
        # candidate.  Re-deriving it from target bytes would reject harmless
        # Markdown formatting changes and, for a pinned rename, would treat the
        # old target as a reconstruction source instead of review context.
        target_translations = accepted_map.as_dict()
    else:
        # A deterministic pinned rename has no accepted map. Its bytes are
        # review context only; a repair must supply a complete new source map.
        target_translations = {}
    accepted_maps: tuple[AcceptedMap, ...] = (
        (AcceptedMap(target_path, tuple(sorted(target_translations.items()))),)
        if accepted_map is not None or not full_repair
        else ()
    )
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
        operator_context=operator_context,
        before_model_call=before_model_call,
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
            accepted_maps,
        )

    repair_requests, document_request, source_blocks, target_blocks = _repair_requests(
        model=model,
        source=source,
        source_plan=source_plan,
        target=target,
        target_path=target_path,
        source_locale=source_locale,
        target_locale=target_locale,
        findings=repair_findings,
        operator_context=operator_context,
        max_characters=max_request_characters,
    )
    repair_error: RepairErrorReason | None = None
    repaired_candidate: bytes | None = None
    effective_chunks: list[DocumentChunk] = []
    responses: list[str] = []

    def child_repair_request(chunk: DocumentChunk) -> ModelRequest:
        prompt = _repair_prompt(
            source_text=chunk.text,
            target_text="".join(target_blocks[chunk.block_start : chunk.block_end]),
            target_path=target_path,
            source_locale=source_locale,
            target_locale=target_locale,
            findings=repair_findings,
            operator_context=operator_context,
        )
        if len(prompt) > max_request_characters:
            raise QualityInputError
        return ModelRequest(ModelRole.REPAIR, model, prompt, None, 8000, target_path)

    def invoke_repair(request: ModelRequest) -> ModelCallResult:
        if before_model_call is not None:
            before_model_call()
        return executor.invoke(request)

    for chunk, repair_request in zip(document_request.chunks, repair_requests, strict=True):
        repair_response = invoke_repair(repair_request)
        if not repair_response.success or repair_response.text is None:
            children = (
                split_content_filter_chunk(
                    chunk,
                    source_blocks,
                    aligned_block_texts=target_blocks,
                )
                if repair_response.failure is AttemptError.CONTENT_FILTER
                else None
            )
            if children is None:
                raise QualityExecutionError("repair")
            for child in children:
                child_response = invoke_repair(child_repair_request(child))
                if not child_response.success or child_response.text is None:
                    raise QualityExecutionError("repair")
                try:
                    validate_chunk_response(
                        child, document_request.placeholders, child_response.text
                    )
                except DocumentTranslationError:
                    repair_error = RepairErrorReason.INVALID_RESPONSE
                    break
                effective_chunks.append(child)
                responses.append(child_response.text)
            if repair_error is not None:
                break
            continue
        try:
            validate_chunk_response(
                chunk, document_request.placeholders, repair_response.text
            )
        except DocumentTranslationError:
            repair_error = RepairErrorReason.INVALID_RESPONSE
            break
        effective_chunks.append(chunk)
        responses.append(repair_response.text)
    if repair_error is None:
        try:
            effective_request = DocumentTranslationRequest(
                tuple(effective_chunks), document_request.placeholders
            )
            repaired_candidate = restore_document(
                source, source_plan, effective_request, tuple(responses)
            )
            target_translations = _derive_target_translations(
                source,
                source_plan,
                translation_request,
                repaired_candidate,
                target_path,
            )
            accepted_maps = (
                AcceptedMap(target_path, tuple(sorted(target_translations.items()))),
            )
        except DocumentTranslationError:
            repair_error = RepairErrorReason.INVALID_RESPONSE
            repaired_candidate = None
        except (AssemblyError, UnicodeError, QualityInputError):
            repair_error = RepairErrorReason.ASSEMBLY_FAILED
            repaired_candidate = None
    final_candidate = repaired_candidate if repaired_candidate is not None else target
    if repaired_candidate is not None and before_repaired_map is not None:
        before_repaired_map(accepted_maps[0])
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
        operator_context=operator_context,
        before_model_call=before_model_call,
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
        accepted_maps,
    )
