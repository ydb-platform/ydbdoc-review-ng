"""Critic successful chunk must commit/push before arbiter (§4.1)."""

from __future__ import annotations

import json

from tests.support.scripted_models import ScriptedModels
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import ModelCallResult
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict


def test_successful_critic_chunk_publishes_before_arbiter() -> None:
    """REQUIREMENTS §4.1: successful critic chunk immediately commit/push."""
    timeline: list[object] = []
    corrected = {"docs/en/a.md": "# Fixed\n"}

    def on_chunk(files: dict[str, bytes]) -> None:
        timeline.append(("push", {path: files[path].decode() for path in sorted(files)}))

    class Tracking(ScriptedModels):
        def invoke(self, request) -> ModelCallResult:
            timeline.append(request.role.value)
            return super().invoke(request)

    models = Tracking(
        [
            json.dumps({"files": {path: text for path, text in corrected.items()}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    result_files, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={"docs/ru/a.md": b"# A\n"},
        translated_files={"docs/en/a.md": b"# Draft\n"},
        glossary_files={},
        validate_files=lambda files: None,
        on_successful_critic_chunk=on_chunk,
    )

    assert timeline[0] == ModelRole.CRITIC.value
    assert ("push", {"docs/en/a.md": "# Fixed\n"}) in timeline
    assert timeline[-1] == ModelRole.ARBITER.value
    push_at = timeline.index(("push", {"docs/en/a.md": "# Fixed\n"}))
    assert all(item == ModelRole.CRITIC.value for item in timeline[:push_at])
    assert result_files == {path: text.encode() for path, text in corrected.items()}
    assert final.verdict is Verdict.GREEN
