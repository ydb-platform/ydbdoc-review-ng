"""One complete-PR critic pass followed by independent arbitration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.models import ModelCallResult, ModelRequest
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind, SourcePlan, fields_of
from ydbdoc_review_ng.quality.critic import (
    build_pr_arbiter_request,
    build_pr_critic_request,
    parse_pr_arbiter_response,
    parse_pr_critic_response,
)
from ydbdoc_review_ng.quality.types import CriticResult
from ydbdoc_review_ng.translation import (
    ProtectedMismatch,
    TranslationRequest,
    build_translation_request,
    verify_document_candidate,
)
from ydbdoc_review_ng.translation.contract import field_request_text


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


def review_pr(
    executor: ModelExecutor,
    *,
    critic_model: str,
    arbiter_model: str,
    source_files: Mapping[str, bytes],
    translated_files: Mapping[str, bytes | None],
    glossary_files: Mapping[str, bytes],
    validate_files: Callable[[Mapping[str, bytes]], None],
    operator_context: str | None = None,
    before_model_call: Callable[[], None] | None = None,
    on_successful_critic_chunk: Callable[[Mapping[str, bytes]], None] | None = None,
) -> tuple[dict[str, bytes], CriticResult]:
    """Correct the complete PR once, validate atomically, then judge those bytes."""
    critic = build_pr_critic_request(
        model=critic_model,
        source_files=source_files,
        translated_files=translated_files,
        glossary_files=glossary_files,
        operator_context=operator_context,
    )
    if before_model_call is not None:
        before_model_call()
    response = executor.invoke(critic)
    if not response.success or response.text is None:
        raise QualityExecutionError("critic")
    corrected = parse_pr_critic_response(response.text, target_paths=tuple(translated_files))
    validate_files(corrected)
    if on_successful_critic_chunk is not None:
        on_successful_critic_chunk(corrected)
    arbiter = build_pr_arbiter_request(
        model=arbiter_model,
        source_files=source_files,
        translated_files=corrected,
        glossary_files=glossary_files,
        operator_context=operator_context,
    )
    if before_model_call is not None:
        before_model_call()
    response = executor.invoke(arbiter)
    if not response.success or response.text is None:
        raise QualityExecutionError("arbiter")
    final = parse_pr_arbiter_response(response.text, target_files=corrected)
    return corrected, final


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
