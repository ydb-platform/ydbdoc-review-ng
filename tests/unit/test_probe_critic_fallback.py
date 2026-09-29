import json

from scripts.probe_critic_fallback import (
    _arbiter_probe_request,
    _diagnostic_probe_request,
    _parse_diagnostic_probe_response,
    _translator_probe_contract,
)
from ydbdoc_review_ng.domain import ModelRole


def test_translator_probe_uses_prose_only_segment_contract() -> None:
    request, field, contract, _segments, document, _plan = _translator_probe_contract(
        "deepseek-v4-flash"
    )

    assert request.role is ModelRole.TRANSLATE
    assert request.schema is not None
    assert set(request.schema["properties"]) == set(contract.requested_ids)
    assert "`ydb`" not in request.prompt
    assert "guide.md" not in request.prompt
    assert len(field.placeholders) == len(document.placeholders) == 2


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
    assert set(schema["properties"]) == {"corrected_markdown"}
    _parse_diagnostic_probe_response(
        json.dumps(
            {
                "corrected_markdown": (
                    "**Storage group**, **distributed storage group**, **storage group**, or "
                    "**Blob storage group** is a place for reliable data storage."
                ),
            }
        )
    )
