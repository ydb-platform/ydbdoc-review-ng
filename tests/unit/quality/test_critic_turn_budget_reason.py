"""TURN_BUDGET must not be mislabeled as provider outage."""

from __future__ import annotations

from tests.support.scripted_models import ScriptedModels
from tests.support.tool_critic_scripts import tool_result
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict


def test_turn_budget_is_reported_as_budget_not_provider(monkeypatch) -> None:
    monkeypatch.setenv("YDBDOC_CRITIC_MAX_TOOL_TURNS", "2")
    source = {"docs/ru/a.md": b"# A\nline\n"}
    translated = {"docs/en/a.md": b"# Draft\nline\n"}
    # max_turns=2: two successful tool turns, third invoke hits TURN_BUDGET.
    # Two full sessions (retry) → six model calls.
    read = tool_result(
        ("read", {"path": "docs/en/a.md", "start_line": 1, "end_line": 1})
    )
    grep = tool_result(("grep", {"pattern": "Draft", "path": "docs/en/a.md"}))
    session = [read, grep, read]
    models = ScriptedModels([*session, *session])

    _corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert all(call.role is ModelRole.CRITIC for call in models.calls)
    assert final.verdict is Verdict.RED
    assert any("лимит tool-ходов" in item.reason for item in final.findings), [
        item.reason for item in final.findings
    ]
    assert not any("сбоя модели или провайдера" in item.reason for item in final.findings)
