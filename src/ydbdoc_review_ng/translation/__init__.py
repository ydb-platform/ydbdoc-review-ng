"""Strict translation boundary and source-only candidate assembly."""

from ydbdoc_review_ng.translation.assembly import (
    AssemblyError,
    AssemblyErrorReason,
    ProtectedMismatch,
    assemble_candidate,
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

__all__ = [
    "AssemblyError",
    "AssemblyErrorReason",
    "Placeholder",
    "ProtectedMismatch",
    "ResponseError",
    "ResponseErrorReason",
    "TranslationField",
    "TranslationRequest",
    "assemble_candidate",
    "build_translation_request",
    "parse_translation_response",
    "verify_protected_fragments",
]
