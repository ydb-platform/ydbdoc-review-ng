"""P1C reporting: null-location findings and zero-commit RED."""

from __future__ import annotations

from decimal import Decimal

from ydbdoc_review_ng.domain import GitSha
from ydbdoc_review_ng.quality import CriticResult, Finding, QualityReviewResult, Verdict
from ydbdoc_review_ng.reporting import ReportContext, render_report


def test_null_location_finding_is_publishable() -> None:
    """REQUIREMENTS §4.2 / §7: missing/unreviewed targets may omit line+snippet (#10)."""
    review = QualityReviewResult(
        b"{}",
        None,
        b"{}",
        CriticResult(Verdict.GREEN, ()),
        CriticResult(
            Verdict.RED,
            (
                Finding(
                    False,
                    "Файл не удалось проверить в доступном контексте модели.",
                    "Уменьшите файл или продолжите проверку отдельно.",
                    None,
                    "ydb/docs/en/core/a.md",
                    None,
                ),
            ),
        ),
        True,
        False,
        None,
        (),
    )
    report = render_report(
        review,
        ReportContext(GitSha("a" * 40), GitSha("b" * 40), Decimal("1.0"), source_pr_number=42),
    )
    assert "🔴 RED" in report
    assert "файл целиком" in report
    assert "ydb/docs/en/core/a.md" in report
