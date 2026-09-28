import json

from scripts.probe_critic_fallback import (
    _diagnostic_probe_request,
    _parse_diagnostic_probe_response,
    _targeted_probe_request,
)
from ydbdoc_review_ng.domain import Locale, RepoPath
from ydbdoc_review_ng.quality.critic import build_critic_request


def test_targeted_probe_rebuilds_frozen_schema_as_json_input() -> None:
    primary = build_critic_request(
        model="yandexgpt-5.1",
        source="Группа хранения".encode(),
        target=b"Storage group, storage group",
        target_path=RepoPath("ydb/docs/en/probe.md"),
        source_locale=Locale.RU,
        target_locale=Locale.EN,
        requested_ids=("document",),
        source_is_excerpt=True,
        target_is_excerpt=True,
        editable=True,
    )

    request = _targeted_probe_request(primary)

    assert request.schema is not None
    assert "Mandatory unresolved edits" in request.prompt
    assert "Remove the duplicate storage group alias" in request.prompt


def test_diagnostic_probe_uses_the_runtime_whole_excerpt_contract() -> None:
    request = _diagnostic_probe_request("critic")
    schema = request.schema

    assert schema is not None
    finding = schema["properties"]["findings"]["items"]
    assert "field_ids" not in finding["properties"]
    assert "field_ids" not in finding["required"]
    _parse_diagnostic_probe_response(
        json.dumps(
            {
                "verdict": "GREEN",
                "findings": [],
                "corrected_markdown": (
                    "**Storage group**, **distributed storage group**, **storage group**, or "
                    "**Blob storage group** is a place for reliable data storage."
                ),
            }
        )
    )
