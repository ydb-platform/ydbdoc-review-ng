"""Bounded quality orchestration: one critic-editor pass per document unit."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Protocol, cast

from ydbdoc_review_ng.continuation import AcceptedMap
from ydbdoc_review_ng.domain import Locale, RepoPath
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.models.types import FrozenJson, mutable_json
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import BlockKind, ProtectedKind, SourcePlan, fields_of
from ydbdoc_review_ng.quality.critic import (
    CriticResponseError,
    build_critic_request,
    parse_critic_response,
)
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
    def __init__(self, reason: str = "inconsistent_candidate") -> None:
        self.reason = reason
        super().__init__(f"quality_input:{reason}")


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
    terminology_context: str | None = None,
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
        terminology_context=terminology_context,
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
    terminology_context: str | None = None,
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
            terminology_context=terminology_context,
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


def _editor_requests_from_draft(
    *,
    model: str,
    source: bytes,
    source_plan: SourcePlan,
    target: bytes,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    document_request: DocumentTranslationRequest,
    draft_responses: tuple[str, ...],
    requested_ids: tuple[str, ...],
    operator_context: str | None,
    max_characters: int,
    terminology_context: str | None = None,
) -> tuple[
    tuple[ModelRequest, ...],
    DocumentTranslationRequest,
    tuple[str, ...],
    tuple[str, ...],
]:
    """Build critic inputs from the exact validated translator chunk pairs."""
    if len(document_request.chunks) != len(draft_responses):
        raise QualityInputError("draft_response_count_mismatch")
    try:
        restored = restore_document(source, source_plan, document_request, draft_responses)
    except (DocumentTranslationError, AssemblyError, UnicodeError, ValueError, TypeError):
        raise QualityInputError("draft_restore_failed") from None
    if restored != target:
        raise QualityInputError("draft_target_mismatch")

    source_blocks = _document_block_texts(
        source, source_plan, document_request.placeholders
    )
    target_blocks = [""] * len(source_blocks)
    requests: list[ModelRequest] = []
    chunks: list[DocumentChunk] = []
    draft_index = 0
    while draft_index < len(document_request.chunks):
        accepted: tuple[DocumentChunk, str, ModelRequest] | None = None
        candidate_end = draft_index + 1
        while candidate_end <= len(document_request.chunks):
            source_chunks = document_request.chunks[draft_index:candidate_end]
            source_text = "".join(item.text for item in source_chunks)
            draft = "".join(draft_responses[draft_index:candidate_end])
            chunk = DocumentChunk(
                source_text,
                source_chunks[0].block_start,
                source_chunks[-1].block_end,
                tuple(token for item in source_chunks for token in item.placeholders),
            )
            request = _editor_request(
                model=model,
                source_text=source_text,
                target_text=draft,
                target_path=target_path,
                source_locale=source_locale,
                target_locale=target_locale,
                requested_ids=requested_ids,
                operator_context=operator_context,
                terminology_context=terminology_context,
            )
            if (
                len(request.prompt) > max_characters
                or max(len(source_text), len(draft))
                > RAW_MARKDOWN_RESPONSE_MAX_CHARACTERS
            ):
                break
            accepted = chunk, draft, request
            candidate_end += 1
        if accepted is None:
            raise QualityInputError("draft_critic_prompt_exceeds_limit")
        chunk, draft, request = accepted
        chunks.append(chunk)
        requests.append(request)

        # Child segments are used only after a provider content-filter refusal.
        # Preserve the exact draft concatenation while assigning deterministic
        # boundaries proportional to the already accepted source blocks.
        block_texts = source_blocks[chunk.block_start : chunk.block_end]
        if not block_texts:
            draft_index = candidate_end - 1
            continue
        lengths = tuple(len(value) for value in block_texts)
        total = sum(lengths)
        boundaries = [0]
        consumed = 0
        for length in lengths[:-1]:
            consumed += length
            wanted = len(draft) * consumed // total if total else 0
            candidates = tuple(
                match.end()
                for match in re.finditer(r"\s+", draft)
                if boundaries[-1] < match.end() < len(draft)
            )
            boundaries.append(
                min(candidates, key=lambda value: abs(value - wanted))
                if candidates
                else wanted
            )
        boundaries.append(len(draft))
        for block_index, start, block_end in zip(
            range(chunk.block_start, chunk.block_end),
            boundaries[:-1],
            boundaries[1:],
            strict=True,
        ):
            target_blocks[block_index] = draft[start:block_end]
        draft_index = candidate_end - 1
    return (
        tuple(requests),
        DocumentTranslationRequest(tuple(chunks), document_request.placeholders),
        source_blocks,
        tuple(target_blocks),
    )


def _fallback_editor_request(request: ModelRequest, fallback_model: str, /) -> ModelRequest:
    fallback_schema = cast(
        FrozenJson,
        {
            "type": "object",
            "properties": {"corrected_markdown": {"type": "string"}},
            "required": ["corrected_markdown"],
            "additionalProperties": False,
        },
    )
    return ModelRequest(
        request.role,
        fallback_model,
        request.prompt
        + "\n\nFallback response contract: return exactly one JSON field named "
        "corrected_markdown containing the complete final corrected target excerpt. This "
        "contract overrides the earlier request for verdict and findings. Return no other "
        "fields and no prose outside JSON.",
        fallback_schema,
        request.max_tokens,
        request.target_path,
    )


def review_translation(
    executor: ModelExecutor,
    *,
    model: str,
    fallback_model: str | None = None,
    source: bytes,
    source_plan: SourcePlan,
    translation_request: TranslationRequest,
    target: bytes,
    target_path: RepoPath,
    source_locale: Locale,
    target_locale: Locale,
    on_validated_edit: Callable[[bytes], None] | None = None,
    accepted_map: AcceptedMap | None = None,
    full_repair: bool = False,
    operator_context: str | None = None,
    before_model_call: Callable[[], None] | None = None,
    before_repaired_map: Callable[[AcceptedMap], None] | None = None,
    max_request_characters: int = 200_000,
    link_resolver: LinkResolver | None = None,
    terminology_context: str | None = None,
    draft_request: DocumentTranslationRequest | None = None,
    draft_responses: tuple[str, ...] | None = None,
) -> QualityReviewResult:
    """Let the critic turn the translator draft into the final validated candidate."""
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
    # Full-excerpt editor responses no longer use the legacy field-level repair
    # protocol. Keeping a synthetic "document" field ID only creates schema
    # combinations that the strict response parser cannot represent.
    critic_field_ids: tuple[str, ...] = ()
    if (draft_request is None) != (draft_responses is None):
        raise QualityInputError("incomplete_draft_chunks")
    if draft_request is None:
        editor_requests, document_request, _source_blocks, target_blocks = _editor_requests(
            model=model,
            source=source,
            source_plan=source_plan,
            target=target,
            target_path=target_path,
            source_locale=source_locale,
            target_locale=target_locale,
            requested_ids=critic_field_ids,
            operator_context=operator_context,
            max_characters=max_request_characters,
            link_resolver=link_resolver,
            terminology_context=terminology_context,
        )
    else:
        assert draft_responses is not None
        editor_requests, document_request, _source_blocks, target_blocks = (
            _editor_requests_from_draft(
                model=model,
                source=source,
                source_plan=source_plan,
                target=target,
                target_path=target_path,
                source_locale=source_locale,
                target_locale=target_locale,
                document_request=draft_request,
                draft_responses=draft_responses,
                requested_ids=critic_field_ids,
                operator_context=operator_context,
                max_characters=max_request_characters,
                terminology_context=terminology_context,
            )
        )
    repair_error: RepairErrorReason | None = None
    repaired_candidate: bytes | None = None
    effective_chunks: list[DocumentChunk] = []
    responses: list[str] = []
    editor_results: list[CriticResult] = []
    unresolved_findings: list[Finding] = []
    editor_changed_target = False

    def invoke_editor(request: ModelRequest) -> ModelCallResult:
        if before_model_call is not None:
            before_model_call()
        return executor.invoke(request)

    def flat_correction(response: ModelCallResult, /) -> str:
        if not response.success or response.text is None:
            raise QualityExecutionError("critic")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            raise QualityExecutionError("critic") from None
        if type(payload) is not dict or set(payload) != {"corrected_markdown"}:
            raise QualityExecutionError("critic")
        correction = payload["corrected_markdown"]
        if type(correction) is not str:
            raise QualityExecutionError("critic")
        return correction

    def accept_editor_response(
        chunk: DocumentChunk, request: ModelRequest, response: ModelCallResult, /
    ) -> None:
        nonlocal editor_changed_target
        if not response.success or response.text is None:
            raise QualityExecutionError("critic")
        current_target = "".join(target_blocks[chunk.block_start : chunk.block_end])
        result = parse_critic_response(
            response.text,
            target_path=target_path,
            requested_ids=critic_field_ids,
            editable=True,
            current_target=current_target,
        )
        proposed_correction = result.corrected_markdown
        assert proposed_correction is not None
        correction = (
            proposed_correction if result.verdict is Verdict.RED else current_target
        )
        pending = tuple(
            finding
            for finding in result.findings
            if not finding.repairable
        )
        if pending:
            before_targeted_edit = correction
            repair_payload = [
                {
                    "reason": finding.reason,
                    "expected_correction": finding.expected_correction,
                    "searchable_snippet": finding.searchable_snippet,
                }
                for finding in pending
            ]
            repair_request = _fallback_editor_request(request, request.model)
            repair_request = ModelRequest(
                repair_request.role,
                repair_request.model,
                repair_request.prompt
                + "\n\nMandatory unresolved edits:\n"
                + json.dumps(repair_payload, ensure_ascii=False)
                + "\nApply every listed expected_correction now and return the complete "
                "changed target excerpt. Do not merely repeat the input. This is the only "
                "correction attempt.",
                None
                if repair_request.schema is None
                else cast(FrozenJson, mutable_json(repair_request.schema)),
                repair_request.max_tokens,
                repair_request.target_path,
            )
            correction = flat_correction(invoke_editor(repair_request))
            if correction != before_targeted_edit:
                pending = ()
        unresolved_findings.extend(pending)
        editor_changed_target = editor_changed_target or correction != current_target
        editor_results.append(result)
        validate_chunk_response(chunk, document_request.placeholders, correction)
        effective_chunks.append(chunk)
        responses.append(correction)

    def accept_fallback_response(
        chunk: DocumentChunk, response: ModelCallResult, /
    ) -> None:
        nonlocal editor_changed_target
        correction = flat_correction(response)
        current_target = "".join(target_blocks[chunk.block_start : chunk.block_end])
        editor_changed_target = editor_changed_target or correction != current_target
        validate_chunk_response(chunk, document_request.placeholders, correction)
        effective_chunks.append(chunk)
        responses.append(correction)
        # The fallback is a final editor, not a second diagnostic judge. Its
        # correction is accepted only after the same structural validation.
        editor_results.append(CriticResult(Verdict.GREEN, ()))

    def process_editor_chunk(chunk: DocumentChunk, request: ModelRequest) -> bool:
        response = invoke_editor(request)
        if (
            (not response.success or response.text is None)
            and response.failure is AttemptError.CONTENT_FILTER
            and fallback_model is not None
            and fallback_model != request.model
        ):
            # Never split translated text proportionally. During doc_translate
            # we still have the exact validated translator chunk pairs, so the
            # alternate editor receives those original bounded units. Verify
            # and continuation have no draft pairs and keep the intact request.
            fallback_units: tuple[tuple[DocumentChunk, ModelRequest], ...]
            if draft_request is not None and draft_responses is not None:
                units = []
                for original, draft_response in zip(
                    draft_request.chunks, draft_responses, strict=True
                ):
                    if (
                        chunk.block_start <= original.block_start
                        and original.block_end <= chunk.block_end
                    ):
                        units.append(
                            (
                                original,
                                _editor_request(
                                    model=fallback_model,
                                    source_text=original.text,
                                    target_text=draft_response,
                                    target_path=target_path,
                                    source_locale=source_locale,
                                    target_locale=target_locale,
                                    requested_ids=critic_field_ids,
                                    operator_context=operator_context,
                                    terminology_context=terminology_context,
                                ),
                            )
                        )
                if (
                    not units
                    or units[0][0].block_start != chunk.block_start
                    or units[-1][0].block_end != chunk.block_end
                    or "".join(item[0].text for item in units) != chunk.text
                ):
                    raise QualityInputError("draft_fallback_alignment_failed")
                fallback_units = tuple(units)
            else:
                fallback_units = ((chunk, request),)
            for fallback_chunk, fallback_request in fallback_units:
                fallback_response = invoke_editor(
                    _fallback_editor_request(fallback_request, fallback_model)
                )
                if not fallback_response.success or fallback_response.text is None:
                    raise QualityExecutionError("critic")
                try:
                    accept_fallback_response(fallback_chunk, fallback_response)
                except DocumentTranslationError:
                    return False
            return True
        if not response.success or response.text is None:
            raise QualityExecutionError("critic")
        try:
            accept_editor_response(chunk, request, response)
        except CriticResponseError as error:
            retry = ModelRequest(
                request.role,
                request.model,
                request.prompt
                + "\n\nYour previous response violated the required JSON contract "
                + f"({error.reason.value}). Return the same review exactly once more, "
                "strictly matching the response schema. Do not add prose outside JSON.",
                None
                if request.schema is None
                else cast(FrozenJson, mutable_json(request.schema)),
                request.max_tokens,
                request.target_path,
            )
            accept_editor_response(chunk, retry, invoke_editor(retry))
        except DocumentTranslationError:
            return False
        return True

    for chunk, editor_request in zip(document_request.chunks, editor_requests, strict=True):
        if not process_editor_chunk(chunk, editor_request):
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
    if primary.verdict is Verdict.GREEN and not editor_changed_target:
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
    if on_validated_edit is not None:
        on_validated_edit(final_candidate)
    # The critic-editor is the final semantic writer. Its valid primary edits
    # resolve repairable findings; one valid changed targeted edit resolves the
    # remaining diagnoses. An unchanged targeted response stays RED. There is no
    # second critique or repair loop.
    unresolved = tuple(dict.fromkeys(unresolved_findings))
    final = CriticResult(Verdict.RED if unresolved else Verdict.GREEN, unresolved)
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
