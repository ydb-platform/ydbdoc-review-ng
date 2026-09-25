"""Bounded quality orchestration: one critic-editor and one final critic."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol

from ydbdoc_review_ng.continuation import AcceptedMap
from ydbdoc_review_ng.domain import Locale, RepoPath
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import BlockKind, ProtectedKind, SourcePlan, fields_of
from ydbdoc_review_ng.quality.critic import build_critic_request, parse_critic_response
from ydbdoc_review_ng.quality.types import (
    CriticResult,
    Finding,
    QualityReviewResult,
    RepairErrorReason,
    Verdict,
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
    LinkResolver,
    _document_block_texts,
    verify_document_candidate_with_links,
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


_CRITIC_REQUEST_MAX_CHARACTERS = 80_000


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
    def request_for(
        source_part: bytes,
        target_part: bytes = target,
        *,
        source_excerpt: bool,
        target_excerpt: bool = False,
    ) -> ModelRequest:
        return build_critic_request(
            model=model,
            source=source_part,
            target=target_part,
            target_path=target_path,
            source_locale=source_locale,
            target_locale=target_locale,
            requested_ids=requested_ids,
            final=final,
            source_is_excerpt=source_excerpt,
            target_is_excerpt=target_excerpt,
            operator_context=operator_context,
        )

    def split_text(text: str) -> tuple[str, str]:
        if len(text) < 2:
            raise QualityExecutionError("final_critic" if final else "critic")
        midpoint = len(text) // 2
        boundaries = [
            boundary
            for marker in ("\n\n", "\n")
            for boundary in (text.rfind(marker, 0, midpoint), text.find(marker, midpoint))
            if 0 < boundary < len(text)
        ]
        boundary = min(boundaries, key=lambda item: abs(item - midpoint), default=midpoint)
        return text[:boundary], text[boundary:]

    request = request_for(source, source_excerpt=False)
    requests: tuple[ModelRequest, ...]
    if len(request.prompt) <= _CRITIC_REQUEST_MAX_CHARACTERS:
        requests = (request,)
    else:
        pending_pairs = [(source.decode("utf-8"), target.decode("utf-8"))]
        pairs: list[tuple[bytes, bytes]] = []
        while pending_pairs:
            source_text, target_text = pending_pairs.pop()
            source_part, target_part = source_text.encode(), target_text.encode()
            pair_request = request_for(
                source_part,
                target_part,
                source_excerpt=True,
                target_excerpt=True,
            )
            if len(pair_request.prompt) <= _CRITIC_REQUEST_MAX_CHARACTERS:
                pairs.append((source_part, target_part))
                continue
            source_left, source_right = split_text(source_text)
            target_left, target_right = split_text(target_text)
            pending_pairs.append((source_right, target_right))
            pending_pairs.append((source_left, target_left))
        requests = tuple(
            request_for(
                source_part,
                target_part,
                source_excerpt=True,
                target_excerpt=True,
            )
            for source_part, target_part in pairs
        )

    results: list[CriticResult] = []
    for item in requests:
        if before_model_call is not None:
            before_model_call()
        response = executor.invoke(item)
        if not response.success or response.text is None:
            raise QualityExecutionError("final_critic" if final else "critic")
        results.append(
            parse_critic_response(
                response.text, target_path=target_path, requested_ids=requested_ids
            )
        )
    findings: list[Finding] = []
    for result in results:
        for finding in result.findings:
            if finding not in findings:
                findings.append(finding)
    verdict = Verdict.RED if any(result.verdict is Verdict.RED for result in results) else Verdict.GREEN
    return CriticResult(verdict, tuple(findings))


def _editor_request(
    *,
    model: str,
    source_text: str,
    target_text: str,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    requested_ids: tuple[str, ...],
    operator_context: str | None = None,
) -> ModelRequest:
    return build_critic_request(
        model=model,
        source=source_text.encode(),
        target=target_text.encode(),
        target_path=target_path,
        source_locale=source_locale,
        target_locale=target_locale,
        requested_ids=requested_ids,
        source_is_excerpt=True,
        target_is_excerpt=True,
        operator_context=operator_context,
        editable=True,
    )


def _editor_requests(
    *,
    model: str,
    source: bytes,
    source_plan: SourcePlan,
    target: bytes,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    requested_ids: tuple[str, ...],
    operator_context: str | None,
    max_characters: int,
    link_resolver: LinkResolver | None = None,
) -> tuple[
    tuple[ModelRequest, ...],
    DocumentTranslationRequest,
    tuple[str, ...],
    tuple[str, ...],
]:
    target_plan = build_markdown_plan(source_plan.source_snapshot, target_path, target)
    try:
        if link_resolver is None:
            verify_document_candidate(source, source_plan, target, target_plan)
        else:
            verify_document_candidate_with_links(
                source, source_plan, target, target_plan, link_resolver
            )
    except (ProtectedMismatch, TypeError, ValueError):
        raise QualityInputError from None
    source_document = prepare_document(
        source,
        source_plan,
        max_characters=2**63 - 1,
        link_resolver=link_resolver,
    )
    target_document = prepare_document(target, target_plan, max_characters=2**63 - 1)
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
    target_token_pattern = (
        re.compile("|".join(re.escape(token) for token in target_replacements))
        if target_replacements
        else None
    )

    def align_target_tokens(block: str) -> str:
        if target_token_pattern is None:
            return block
        return target_token_pattern.sub(
            lambda match: target_replacements[match.group(0)], block
        )

    target_blocks = tuple(
        align_target_tokens(block)
        for block in _document_block_texts(target, target_plan, target_document.placeholders)
    )

    if len(source_plan.blocks) != len(target_plan.blocks):
        target_text = "".join(target_blocks)
        content_positions = tuple(
            index
            for index, block in enumerate(source_plan.blocks)
            if block.kind is not BlockKind.BLANK
        )
        if not content_positions:
            raise QualityInputError
        source_lengths = tuple(len(source_blocks[index]) for index in content_positions)
        source_total = sum(source_lengths)
        boundaries = [0]
        consumed = 0
        for length in source_lengths[:-1]:
            consumed += length
            wanted = len(target_text) * consumed // source_total
            candidates = tuple(
                match.end()
                for match in re.finditer(r"\s+", target_text)
                if boundaries[-1] < match.end() < len(target_text)
            )
            boundaries.append(
                min(candidates, key=lambda value: abs(value - wanted))
                if candidates
                else wanted
            )
        boundaries.append(len(target_text))
        aligned = [""] * len(source_blocks)
        for position, start, end in zip(
            content_positions, boundaries[:-1], boundaries[1:], strict=True
        ):
            aligned[position] = target_text[start:end]
        target_blocks = tuple(aligned)

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
        request = _editor_request(
            model=model,
            source_text=source_text,
            target_text=target_text,
            target_path=target_path,
            source_locale=source_locale,
            target_locale=target_locale,
            requested_ids=requested_ids,
            operator_context=operator_context,
        )
        return chunk, request

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
                len("".join(source_blocks[start:end]))
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
    link_resolver: LinkResolver | None = None,
) -> QualityReviewResult:
    """Let one critic edit the candidate, then independently review any correction."""
    if accepted_map is None and not full_repair:
        try:
            target_translations = _derive_target_translations(
                source, source_plan, translation_request, target, target_path
            )
        except QualityInputError:
            target_translations = {}
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
    if not allow_repair:
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
    editor_requests, document_request, source_blocks, target_blocks = _editor_requests(
        model=model,
        source=source,
        source_plan=source_plan,
        target=target,
        target_path=target_path,
        source_locale=source_locale,
        target_locale=target_locale,
        requested_ids=translation_request.requested_ids,
        operator_context=operator_context,
        max_characters=max_request_characters,
        link_resolver=link_resolver,
    )
    repair_error: RepairErrorReason | None = None
    repaired_candidate: bytes | None = None
    effective_chunks: list[DocumentChunk] = []
    responses: list[str] = []
    editor_results: list[CriticResult] = []

    def child_editor_request(chunk: DocumentChunk) -> ModelRequest:
        request = _editor_request(
            model=model,
            source_text=chunk.text,
            target_text="".join(target_blocks[chunk.block_start : chunk.block_end]),
            target_path=target_path,
            source_locale=source_locale,
            target_locale=target_locale,
            requested_ids=translation_request.requested_ids,
            operator_context=operator_context,
        )
        if len(request.prompt) > max_request_characters:
            raise QualityInputError
        return request

    def invoke_editor(request: ModelRequest) -> ModelCallResult:
        if before_model_call is not None:
            before_model_call()
        return executor.invoke(request)

    def accept_editor_response(
        chunk: DocumentChunk, response: ModelCallResult, /
    ) -> None:
        if not response.success or response.text is None:
            raise QualityExecutionError("critic")
        current_target = "".join(target_blocks[chunk.block_start : chunk.block_end])
        result = parse_critic_response(
            response.text,
            target_path=target_path,
            requested_ids=translation_request.requested_ids,
            editable=True,
            current_target=current_target,
        )
        correction = result.corrected_markdown
        assert correction is not None
        editor_results.append(result)
        validate_chunk_response(chunk, document_request.placeholders, correction)
        effective_chunks.append(chunk)
        responses.append(correction)

    for chunk, editor_request in zip(document_request.chunks, editor_requests, strict=True):
        editor_response = invoke_editor(editor_request)
        if not editor_response.success or editor_response.text is None:
            children = (
                split_content_filter_chunk(
                    chunk,
                    source_blocks,
                    aligned_block_texts=target_blocks,
                )
                if editor_response.failure is AttemptError.CONTENT_FILTER
                else None
            )
            if children is None:
                raise QualityExecutionError("critic")
            for child in children:
                child_response = invoke_editor(child_editor_request(child))
                try:
                    accept_editor_response(child, child_response)
                except DocumentTranslationError:
                    repair_error = RepairErrorReason.INVALID_RESPONSE
                    break
            if repair_error is not None:
                break
            continue
        try:
            accept_editor_response(chunk, editor_response)
        except DocumentTranslationError:
            repair_error = RepairErrorReason.INVALID_RESPONSE
            break

    findings: list[Finding] = []
    for result in editor_results:
        for finding in result.findings:
            if finding not in findings:
                findings.append(finding)
    primary = CriticResult(
        Verdict.RED
        if any(result.verdict is Verdict.RED for result in editor_results)
        else Verdict.GREEN,
        tuple(findings),
    )
    if primary.verdict is Verdict.GREEN:
        return QualityReviewResult(
            target,
            None,
            target,
            primary,
            primary,
            False,
            False,
            repair_error,
            accepted_maps,
        )

    if repair_error is None:
        try:
            effective_request = DocumentTranslationRequest(
                tuple(effective_chunks), document_request.placeholders
            )
            repaired_candidate = restore_document(
                source, source_plan, effective_request, tuple(responses)
            )
            if repaired_candidate == target:
                repaired_candidate = None
                return QualityReviewResult(
                    target,
                    None,
                    target,
                    primary,
                    primary,
                    True,
                    False,
                    repair_error,
                    accepted_maps,
                )
            try:
                target_translations = _derive_target_translations(
                    source,
                    source_plan,
                    translation_request,
                    repaired_candidate,
                    target_path,
                )
            except QualityInputError:
                target_translations = accepted_map.as_dict() if accepted_map is not None else {}
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
    if repaired_candidate is None:
        return QualityReviewResult(
            target,
            None,
            target,
            primary,
            primary,
            True,
            False,
            repair_error,
            accepted_maps,
        )
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
