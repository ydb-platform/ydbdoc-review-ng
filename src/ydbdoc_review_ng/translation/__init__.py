"""Strict translation boundary and source-only candidate assembly."""

from ydbdoc_review_ng.translation.assembly import (
    AssemblyError,
    AssemblyErrorReason,
    ProtectedMismatch,
    assemble_candidate,
    validate_translation_values,
    verify_protected_fragments,
)
from ydbdoc_review_ng.translation.contract import (
    Placeholder,
    ResponseError,
    ResponseErrorReason,
    TranslationField,
    TranslationRequest,
    build_translation_request,
    parse_translation_response,
)
from ydbdoc_review_ng.translation.document import (
    DocumentChunk,
    DocumentPlaceholder,
    DocumentTranslationError,
    DocumentTranslationRequest,
    build_document_correction_note,
    build_document_critic_prompt,
    build_document_prompt,
    document_operator_guidance,
    document_placeholder_context,
    prepare_document,
    restore_document,
    validate_chunk_response,
    verify_document_candidate,
)
from ydbdoc_review_ng.translation.presentation import (
    PresentationStyle,
    apply_presentation_map,
    build_presentation_map,
)
from ydbdoc_review_ng.translation.split_backtick import (
    count_split_backtick_identifiers,
    normalize_split_backtick_identifiers,
)
from ydbdoc_review_ng.translation.surgical import (
    SurgicalHunk,
    SurgicalMode,
    SurgicalPlan,
    apply_unique_replacements,
    plan_surgical_update,
)

__all__ = [
    "AssemblyError",
    "AssemblyErrorReason",
    "DocumentChunk",
    "DocumentPlaceholder",
    "DocumentTranslationError",
    "DocumentTranslationRequest",
    "Placeholder",
    "PresentationStyle",
    "ProtectedMismatch",
    "ResponseError",
    "ResponseErrorReason",
    "SurgicalHunk",
    "SurgicalMode",
    "SurgicalPlan",
    "TranslationField",
    "TranslationRequest",
    "apply_presentation_map",
    "apply_unique_replacements",
    "assemble_candidate",
    "build_document_correction_note",
    "build_document_critic_prompt",
    "build_document_prompt",
    "build_presentation_map",
    "build_translation_request",
    "count_split_backtick_identifiers",
    "document_operator_guidance",
    "document_placeholder_context",
    "normalize_split_backtick_identifiers",
    "parse_translation_response",
    "plan_surgical_update",
    "prepare_document",
    "restore_document",
    "validate_chunk_response",
    "validate_translation_values",
    "verify_document_candidate",
    "verify_protected_fragments",
]
