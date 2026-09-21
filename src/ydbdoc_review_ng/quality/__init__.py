"""Whole-document model critic and bounded repair API."""

from ydbdoc_review_ng.quality.critic import (
    CriticResponseError,
    CriticResponseErrorReason,
    build_critic_request,
    critic_schema,
    parse_critic_response,
)
from ydbdoc_review_ng.quality.repair import (
    ModelExecutor,
    QualityExecutionError,
    QualityInputError,
    review_translation,
)
from ydbdoc_review_ng.quality.types import (
    CriticResult,
    Finding,
    QualityReviewResult,
    RepairErrorReason,
    Verdict,
)

__all__ = [
    "CriticResponseError",
    "CriticResponseErrorReason",
    "CriticResult",
    "Finding",
    "ModelExecutor",
    "QualityExecutionError",
    "QualityInputError",
    "QualityReviewResult",
    "RepairErrorReason",
    "Verdict",
    "build_critic_request",
    "critic_schema",
    "parse_critic_response",
    "review_translation",
]
