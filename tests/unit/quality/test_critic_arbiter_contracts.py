"""P1B: critic/arbiter production contracts vs REQUIREMENTS §4."""

from __future__ import annotations

import json
from typing import cast
from unittest.mock import MagicMock

import pytest

from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict
from ydbdoc_review_ng.runtime import RecordedModels


class _Scripted:
    def __init__(self, payloads: list[str | ModelCallResult]) -> None:
        self.payloads = list(payloads)
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest) -> ModelCallResult:
        self.calls.append(request)
        item = self.payloads.pop(0)
        if isinstance(item, ModelCallResult):
            return item
        return ModelCallResult(item, None, ())


def test_recorded_models_exposes_prepare_request_for_chunk_fits() -> None:
    """REQUIREMENTS §4: production packing must use real wire prepare_request (#2)."""
    models = RecordedModels(
        {"YANDEX_API_KEY": "secret", "YANDEX_FOLDER_ID": "folder"},
        cast(object, MagicMock()),
        cast(object, MagicMock()),
    )
    models.bind_job("job-1")
    request = ModelRequest(
        ModelRole.CRITIC,
        "deepseek-v4-flash",
        "x" * 100,
        {
            "type": "object",
            "properties": {"files": {"type": "object", "properties": {}, "required": []}},
            "required": ["files"],
            "additionalProperties": False,
        },
        None,
    )
    budget = models.prepare_request(request)
    assert budget.body  # wire body exists; ValueError would mean does-not-fit


def test_critic_failure_retries_once_then_passes_files_to_arbiter() -> None:
    """REQUIREMENTS §4.1: one retry; files go to arbiter as-is; critic fail ≠ RED (#4)."""
    source = {"docs/ru/a.md": b"# A\n"}
    translated = {"docs/en/a.md": b"# Draft\n"}
    models = _Scripted(
        [
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert [call.role for call in models.calls] == [
        ModelRole.CRITIC,
        ModelRole.CRITIC,
        ModelRole.ARBITER,
    ]
    assert corrected == translated
    assert final.verdict is Verdict.GREEN
    assert final.findings == ()


def test_zero_text_pairs_still_invoke_critic_and_arbiter() -> None:
    """REQUIREMENTS §4.1: empty files object still calls critic + arbiter (#15)."""
    models = _Scripted(
        [
            json.dumps({"files": {}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={},
        translated_files={},
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.ARBITER]
    assert corrected == {}
    assert final.verdict is Verdict.GREEN


def test_critic_non_final_marks_chunk_unreviewed_red() -> None:
    """REQUIREMENTS §4: critic NON_FINAL → unreviewed → RED, not arbiter GREEN."""
    source = {"docs/ru/a.md": b"# A\n"}
    translated = {"docs/en/a.md": b"# Draft\n"}
    models = _Scripted(
        [
            ModelCallResult(None, AttemptError.NON_FINAL, ()),
            ModelCallResult(None, AttemptError.NON_FINAL, ()),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.CRITIC]
    assert corrected == translated
    assert final.verdict is Verdict.RED
    assert final.findings
    assert final.findings[0].target_path == "docs/en/a.md"


def test_arbiter_non_final_produces_unreviewed_red_report() -> None:
    """REQUIREMENTS §4: arbiter NON_FINAL → RED with unreviewed finding, not abort."""
    source = {"docs/ru/a.md": b"# A\n"}
    translated = {"docs/en/a.md": b"# Draft\n"}
    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# Draft\n"}}),
            ModelCallResult(None, AttemptError.NON_FINAL, ()),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.ARBITER]
    assert final.verdict is Verdict.RED
    assert final.findings
    assert final.findings[0].target_path == "docs/en/a.md"


def test_single_arbiter_invalid_finding_produces_unreviewed_red_report() -> None:
    """A malformed locator cannot abort the only arbiter chunk without a QA verdict."""
    source = {"docs/ru/a.md": b"# A\n"}
    translated = {"docs/en/a.md": b"# Draft\n"}
    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# Draft\n"}}),
            json.dumps(
                {
                    "verdict": "YELLOW",
                    "findings": [
                        {
                            "target_path": "docs/en/a.md",
                            "target_line": 1,
                            "searchable_snippet": "text absent from the target line",
                            "reason": "Осталась ошибка перевода.",
                            "expected_correction": "Исправить перевод по исходному тексту.",
                        }
                    ],
                }
            ),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert corrected == translated
    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.ARBITER]
    assert final.verdict is Verdict.RED
    assert len(final.findings) == 1
    assert final.findings[0].target_path == "docs/en/a.md"
    assert final.findings[0].target_line is None
    assert final.findings[0].searchable_snippet is None


def test_single_arbiter_transport_failure_produces_unreviewed_red_report() -> None:
    """A transport failure cannot abort the only arbiter chunk without a QA verdict."""
    source = {"docs/ru/a.md": b"# A\n"}
    translated = {"docs/en/a.md": b"# Draft\n"}
    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# Draft\n"}}),
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert corrected == translated
    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.ARBITER]
    assert final.verdict is Verdict.RED
    assert len(final.findings) == 1
    assert final.findings[0].target_path == "docs/en/a.md"
    assert final.findings[0].target_line is None
    assert final.findings[0].searchable_snippet is None


def test_zero_text_arbiter_non_final_is_red_not_green() -> None:
    """REQUIREMENTS §4: resource-only NON_FINAL must not invent GREEN (#2)."""
    models = _Scripted(
        [
            json.dumps({"files": {}}),
            ModelCallResult(None, AttemptError.NON_FINAL, ()),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={},
        translated_files={},
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.ARBITER]
    assert corrected == {}
    assert final.verdict is Verdict.RED
    assert final.findings


def test_zero_text_critic_non_final_is_red_not_green() -> None:
    """REQUIREMENTS §4: empty-pair critic NON_FINAL → RED, never arbiter GREEN."""
    models = _Scripted(
        [
            ModelCallResult(None, AttemptError.NON_FINAL, ()),
            ModelCallResult(None, AttemptError.NON_FINAL, ()),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={},
        translated_files={},
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.CRITIC]
    assert final.verdict is Verdict.RED
    assert final.findings
