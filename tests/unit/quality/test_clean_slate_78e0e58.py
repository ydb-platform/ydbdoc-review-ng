"""Clean-slate review 78e0e58: critic/arbiter contract closeouts."""

from __future__ import annotations

import json

from tests.support.scripted_models import ScriptedModels
from ydbdoc_review_ng.models import AttemptError, ModelCallResult
from ydbdoc_review_ng.quality.critic import build_pr_arbiter_request, parse_pr_arbiter_response
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict


class _Scripted(ScriptedModels):
    pass


def test_missing_required_target_cannot_finish_green() -> None:
    """§4.2: дыра (null required target) → RED even if arbiter returns GREEN."""
    models = _Scripted(
        [
            ModelCallResult(None, AttemptError.HTTP_STATUS, ()),
            ModelCallResult(None, AttemptError.HTTP_STATUS, ()),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )
    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={"docs/ru/a.md": b"# Source"},
        translated_files={"docs/en/a.md": None},
        glossary_files={},
        validate_files=lambda files: None,
    )
    assert corrected == {}
    assert final.verdict is Verdict.RED
    assert final.findings
    assert final.findings[0].target_path == "docs/en/a.md"
    assert final.findings[0].target_line is None


def test_non_final_then_provider_error_keeps_chunk_unreviewed() -> None:
    """§4: first NON_FINAL is not forgotten when retry fails differently."""
    models = _Scripted(
        [
            ModelCallResult(None, AttemptError.NON_FINAL, ()),
            ModelCallResult(None, AttemptError.HTTP_STATUS, ()),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )
    _corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={"docs/ru/a.md": b"# A\n"},
        translated_files={"docs/en/a.md": b"# Draft\n"},
        glossary_files={},
        validate_files=lambda files: None,
    )
    assert final.verdict is Verdict.RED
    assert final.findings[0].target_path == "docs/en/a.md"


def test_resource_only_arbiter_allows_binary_manifest_finding() -> None:
    """§4.1/§4.2: binary-only review must accept RED finding on manifest path."""
    manifest = {"ydb/docs/en/core/_assets/a.png": {"action": "copy", "source_path": "x"}}
    request = build_pr_arbiter_request(
        model="arbiter",
        source_files={},
        translated_files={},
        glossary_files={},
        binary_manifest=manifest,
    )
    enum = request.schema["properties"]["findings"]["items"]["properties"]["target_path"]["enum"]
    assert "ydb/docs/en/core/_assets/a.png" in enum
    result = parse_pr_arbiter_response(
        json.dumps(
            {
                "verdict": "RED",
                "findings": [
                    {
                        "target_path": "ydb/docs/en/core/_assets/a.png",
                        "searchable_snippet": None,
                        "reason": "Ресурс скопирован без проверки содержимого.",
                        "expected_correction": "Проверить бинарный ресурс вручную.",
                    }
                ],
            }
        ),
        target_files={"ydb/docs/en/core/_assets/a.png": None},
    )
    assert result.verdict is Verdict.RED
    assert result.findings[0].target_path == "ydb/docs/en/core/_assets/a.png"
