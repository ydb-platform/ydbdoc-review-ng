"""Critic/arbiter split oversized reviews into whole file-pair chunks (§4)."""

from __future__ import annotations

import json

from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import ModelCallResult, ModelRequest
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict


class _Scripted:
    def __init__(self, payloads: list[str]) -> None:
        self.payloads = list(payloads)
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest) -> ModelCallResult:
        self.calls.append(request)
        return ModelCallResult(self.payloads.pop(0), None, ())


def _target_paths(request: ModelRequest) -> list[str]:
    files = request.schema["properties"]["files"]
    return list(files["required"])


def test_oversized_review_splits_into_whole_file_pair_chunks_and_pushes_each() -> None:
    """One request that does not fit → chunks by whole pairs; each success pushes."""
    source = {
        "docs/ru/a.md": b"# A\n",
        "docs/ru/b.md": b"# B\n",
    }
    translated = {
        "docs/en/a.md": b"# Draft A\n",
        "docs/en/b.md": b"# Draft B\n",
    }
    pushes: list[tuple[str, ...]] = []

    def fits(request: ModelRequest) -> bool:
        if request.role is ModelRole.CRITIC:
            return len(_target_paths(request)) <= 1
        findings_enum = request.schema["properties"]["findings"]["items"]["properties"][
            "target_path"
        ]["enum"]
        return len(list(findings_enum)) <= 1

    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# Fixed A\n"}}),
            json.dumps({"files": {"docs/en/b.md": "# Fixed B\n"}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
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
        on_successful_critic_chunk=lambda files: pushes.append(tuple(sorted(files))),
        request_fits=fits,
    )

    assert corrected == {
        "docs/en/a.md": b"# Fixed A\n",
        "docs/en/b.md": b"# Fixed B\n",
    }
    assert [call.role for call in models.calls] == [
        ModelRole.CRITIC,
        ModelRole.CRITIC,
        ModelRole.ARBITER,
        ModelRole.ARBITER,
    ]
    assert pushes == [("docs/en/a.md",), ("docs/en/b.md",)]
    assert final.verdict is Verdict.GREEN


def test_single_pair_that_does_not_fit_is_left_unreviewed_and_forces_red() -> None:
    """Pair that never fits stays as-is for arbiter; overall verdict is RED."""
    source = {
        "docs/ru/a.md": b"# A\n",
        "docs/ru/huge.md": b"# Huge\n",
    }
    translated = {
        "docs/en/a.md": b"# Draft A\n",
        "docs/en/huge.md": b"# Draft Huge\n",
    }

    def fits(request: ModelRequest) -> bool:
        if request.role is ModelRole.CRITIC:
            return _target_paths(request) == ["docs/en/a.md"]
        paths = request.schema["properties"]["findings"]["items"]["properties"]["target_path"][
            "enum"
        ]
        return list(paths) == ["docs/en/a.md"]

    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# Fixed A\n"}}),
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
        request_fits=fits,
    )

    assert corrected == {
        "docs/en/a.md": b"# Fixed A\n",
        "docs/en/huge.md": b"# Draft Huge\n",
    }
    assert final.verdict is Verdict.RED
    assert any(
        finding.target_path == "docs/en/huge.md"
        and finding.target_line is None
        and finding.searchable_snippet is None
        and finding.reason == "Файл не удалось проверить в доступном контексте модели."
        and "Уменьш" in finding.expected_correction
        for finding in final.findings
    )
    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.ARBITER]
