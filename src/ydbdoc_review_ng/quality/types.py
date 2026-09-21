"""Public, human-readable quality-review values."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    GREEN = "GREEN"
    RED = "RED"


@dataclass(frozen=True, slots=True)
class Finding:
    repairable: bool
    reason: str
    expected_correction: str
    searchable_snippet: str
    target_path: str
    target_line: int
    field_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CriticResult:
    verdict: Verdict
    findings: tuple[Finding, ...]


class RepairErrorReason(str, Enum):
    MODEL_CALL_FAILED = "model_call_failed"
    INVALID_RESPONSE = "invalid_response"
    ASSEMBLY_FAILED = "assembly_failed"


@dataclass(frozen=True, slots=True)
class QualityReviewResult:
    original_candidate: bytes = field(repr=False)
    repaired_candidate: bytes | None = field(repr=False)
    final_candidate: bytes = field(repr=False)
    primary: CriticResult
    final: CriticResult
    repair_attempted: bool
    repair_applied: bool
    repair_error: RepairErrorReason | None
