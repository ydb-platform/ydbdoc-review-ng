import json

from scripts.probe_critic_fallback import (
    _arbiter_probe_request,
    _diagnostic_probe_request,
    _parse_diagnostic_probe_response,
)
from ydbdoc_review_ng.domain import ModelRole


def test_arbiter_probe_uses_read_only_final_contract() -> None:
    request = _arbiter_probe_request(
        "deepseek-v4-flash",
        "**Storage group** or **Blob storage group** stores data.",
    )

    assert request.role is ModelRole.ARBITER
    assert request.schema is not None
    assert set(request.schema["properties"]) == {"verdict", "findings"}
    assert "corrected_markdown" not in request.schema["properties"]


def test_diagnostic_probe_uses_the_runtime_whole_excerpt_contract() -> None:
    request = _diagnostic_probe_request("critic")
    schema = request.schema

    assert schema is not None
    assert set(schema["properties"]) == {"findings", "corrected_markdown"}
    finding = schema["properties"]["findings"]["items"]
    assert set(finding["properties"]) == {
        "reason",
        "expected_correction",
        "searchable_snippet",
    }
    assert tuple(finding["required"]) == (
        "reason",
        "expected_correction",
        "searchable_snippet",
    )
    _parse_diagnostic_probe_response(
        json.dumps(
            {
                "findings": [],
                "corrected_markdown": (
                    "**Storage group**, **distributed storage group**, **storage group**, or "
                    "**Blob storage group** is a place for reliable data storage."
                ),
            }
        )
    )
