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
        {"ydb/docs/en/probe.md": b"**Storage group** or **Blob storage group** stores data."},
    )

    assert request.role is ModelRole.ARBITER
    assert request.schema is not None
    assert set(request.schema["properties"]) == {"verdict", "findings"}
    assert "corrected_markdown" not in request.schema["properties"]


def test_diagnostic_probe_uses_the_runtime_complete_files_contract() -> None:
    request = _diagnostic_probe_request("critic")
    schema = request.schema

    assert schema is not None
    assert set(schema["properties"]) == {"files"}
    corrected = _parse_diagnostic_probe_response(
        json.dumps(
            {
                "files": {
                    "ydb/docs/en/probe.md": "**Storage group** stores data.\n",
                },
            }
        )
    )
    assert corrected == {"ydb/docs/en/probe.md": b"**Storage group** stores data.\n"}
    final = _arbiter_probe_request("arbiter", corrected)

    def payload(prompt, tag):
        return json.loads(prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0])

    for tag in ("source-pr-files", "project-glossary"):
        assert payload(request.prompt, tag) == payload(final.prompt, tag)
    translated = final.prompt.split("<translation-pr-files>\n", 1)[1]
    assert json.loads(translated.split("\n</translation-pr-files>", 1)[0]) == {
        "ydb/docs/en/probe.md": "**Storage group** stores data.\n"
    }
