from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from ydbdoc_review_ng import quality
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import AttemptError, ModelCallResult


class FifoModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, request, /):
        assert self.responses, "unexpected extra model call"
        self.calls.append(request)
        response = self.responses.pop(0)
        return (
            response
            if isinstance(response, ModelCallResult)
            else ModelCallResult(response, None, ())
        )


def prompt_map(request, tag):
    return json.loads(request.prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0])


@pytest.mark.parametrize("verdict", ["GREEN", "YELLOW", "RED"])
@pytest.mark.parametrize("changed", [False, True])
def test_two_file_pr_has_exactly_one_critic_then_one_arbiter(verdict, changed):
    original = {"en/a.md": b"# Depot\n", "en/b.md": b"# Depot\n"}
    corrected = {path: (b"# BlobDepot\n" if changed else text) for path, text in original.items()}
    findings = (
        []
        if verdict == "GREEN"
        else [
            {
                "target_path": "en/a.md",
                "target_line": 1,
                "searchable_snippet": corrected["en/a.md"].decode().splitlines()[0],
                "reason": "Meaning needs a clearer term.",
                "expected_correction": "Use the source term exactly.",
            }
        ]
    )
    executor = FifoModels(
        [
            json.dumps({"files": {path: text.decode() for path, text in corrected.items()}}),
            json.dumps({"verdict": verdict, "findings": findings}),
        ]
    )
    events = []

    def validate(files: Mapping[str, bytes]):
        assert len(executor.calls) == 1
        assert files == corrected
        events.append("validated")

    def before():
        events.append("call")

    output, final = quality.review_pr(
        executor,
        critic_model="editor",
        arbiter_model="judge",
        source_files={"ru/a.md": b"# BlobDepot\n", "ru/b.md": b"# BlobDepot\n"},
        translated_files=original,
        glossary_files={},
        validate_files=validate,
        before_model_call=before,
    )
    assert output == corrected
    assert final.verdict.value == verdict
    assert events == ["call", "validated", "call"]
    assert [call.role for call in executor.calls] == [ModelRole.CRITIC, ModelRole.ARBITER]
    assert [call.model for call in executor.calls] == ["editor", "judge"]
    assert prompt_map(executor.calls[1], "translation-pr-files") == {
        path: text.decode() for path, text in corrected.items()
    }
    assert executor.responses == []


def test_invalid_files_are_not_applied_and_arbiter_is_not_called():
    original = {"en/a.md": b"# Before\n"}
    executor = FifoModels(['{"files":{"en/a.md":"# After\\n"}}'])

    def reject(_files):
        raise quality.QualityInputError("protected")

    with pytest.raises(quality.QualityInputError, match="protected"):
        quality.review_pr(
            executor,
            critic_model="editor",
            arbiter_model="judge",
            source_files={"ru/a.md": b"# Source\n"},
            translated_files=original,
            glossary_files={},
            validate_files=reject,
        )
    assert original == {"en/a.md": b"# Before\n"}
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        '{"verdict":"RED","findings":[]}',
        ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
    ],
)
def test_critic_failure_raises_without_synthetic_red_or_retry(response):
    executor = FifoModels([response])
    with pytest.raises((quality.CriticResponseError, quality.QualityExecutionError)):
        quality.review_pr(
            executor,
            critic_model="editor",
            arbiter_model="judge",
            source_files={"ru/a.md": b"# Source\n"},
            translated_files={"en/a.md": b"# Before\n"},
            glossary_files={},
            validate_files=lambda files: None,
        )
    assert len(executor.calls) == 1


def test_large_complete_input_is_not_split_or_glossary_filtered():
    full = "# BlobDepot\n\n" + "Complete paragraph.\n\n" * 15000
    glossary = ("Term definition.\n" * 2000) + "Glossary tail.\n"
    executor = FifoModels(
        [
            json.dumps({"files": {"en/a.md": full, "en/b.md": full}}),
            '{"verdict":"GREEN","findings":[]}',
        ]
    )
    quality.review_pr(
        executor,
        critic_model="editor",
        arbiter_model="judge",
        source_files={"ru/a.md": full.encode(), "ru/b.md": full.encode()},
        translated_files={"en/a.md": full.encode(), "en/b.md": full.encode()},
        glossary_files={"glossary.md": glossary.encode()},
        validate_files=lambda files: None,
    )
    assert len(executor.calls) == 2
    for call in executor.calls:
        assert prompt_map(call, "source-pr-files") == {"ru/a.md": full, "ru/b.md": full}
        assert prompt_map(call, "translation-pr-files") == {"en/a.md": full, "en/b.md": full}
        assert prompt_map(call, "project-glossary") == {"glossary.md": glossary}
