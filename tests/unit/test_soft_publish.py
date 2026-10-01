"""Soft-publish: translator successes publish before critic; failures are null."""

from __future__ import annotations

import json
from decimal import Decimal

from ydbdoc_review_ng.application import TranslateWorkflowInput, WorkflowResult
from ydbdoc_review_ng.application.workflows import LinearWorkflows
from ydbdoc_review_ng.domain import Mode
from ydbdoc_review_ng.quality import Verdict, build_pr_critic_request
from ydbdoc_review_ng.runtime_content import pack, unpack

from tests.unit.application.test_workflows import (
    INITIAL_SHA,
    FakeClock,
    FakeContent,
    FakePersistence,
    FakePublisher,
    FakeReporter,
    FakeReviewer,
    FakeSource,
    Scenario,
    SOURCE_SHA,
)


def test_translate_publishes_assembled_candidate_before_critic_review() -> None:
    """REQUIREMENTS §5.1 step 5 then 6: soft-publish, then critic/arbiter."""
    scenario = Scenario()
    persistence = FakePersistence(scenario)
    workflows = LinearWorkflows(
        clock=FakeClock(),
        persistence=persistence,
        source=FakeSource(scenario),
        content=FakeContent(scenario),
        reviewer=FakeReviewer(scenario),
        publisher=FakePublisher(scenario),
        reporter=FakeReporter(scenario),
    )

    result = workflows.doc_translate(TranslateWorkflowInput(42, SOURCE_SHA, Decimal(100)))

    assert result == WorkflowResult("job-42", Mode.DOC_TRANSLATE, INITIAL_SHA, Verdict.GREEN, False)
    assert scenario.events == [
        "job:start",
        "authorize:translate",
        "snapshot:translate",
        "budget",
        "prepare:direction-scope-translate-assemble-reparse",
        "validate:initial",
        "publish:initial",
        "review:t011",
        "review:critic-editor",
        "report:current-pr-verdict",
        "job:finish:succeeded",
    ]
    assert scenario.published_branches == ["translation/pr-42"]
    publish_at = scenario.events.index("publish:initial")
    review_at = scenario.events.index("review:t011")
    assert publish_at < review_at


def test_pack_round_trips_null_failed_targets() -> None:
    """Failed translator paths stay in the candidate as JSON null for critic."""
    packed = pack({"ydb/docs/en/core/a.md": b"# Ok\n", "ydb/docs/en/core/b.md": None})
    assert unpack(packed) == {
        "ydb/docs/en/core/a.md": b"# Ok\n",
        "ydb/docs/en/core/b.md": None,
    }


def test_critic_prompt_renders_null_for_missing_target() -> None:
    request = build_pr_critic_request(
        model="critic-model",
        source_files={"ydb/docs/ru/core/a.md": b"# A\n", "ydb/docs/ru/core/b.md": b"# B\n"},
        translated_files={"ydb/docs/en/core/a.md": b"# A\n", "ydb/docs/en/core/b.md": None},
        glossary_files={},
    )
    files = json.loads(
        request.prompt.split("<translation-pr-files>\n", 1)[1].split(
            "\n</translation-pr-files>", 1
        )[0]
    )
    assert files == {"ydb/docs/en/core/a.md": "# A\n", "ydb/docs/en/core/b.md": None}
    schema_files = request.schema["properties"]["files"]
    assert list(schema_files["required"]) == [
        "ydb/docs/en/core/a.md",
        "ydb/docs/en/core/b.md",
    ]
    assert schema_files["properties"]["ydb/docs/en/core/b.md"] == {"type": "string"}
