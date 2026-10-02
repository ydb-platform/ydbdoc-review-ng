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
def test_two_file_pr_reviews_one_pair_per_critic_and_arbiter_call(verdict, changed):
    original = {"en/a.md": b"# Depot\n", "en/b.md": b"# Depot\n"}
    corrected = {path: (b"# BlobDepot\n" if changed else text) for path, text in original.items()}
    findings = (
        []
        if verdict == "GREEN"
        else [
            {
                "target_path": "en/a.md",
                "searchable_snippet": corrected["en/a.md"].decode().splitlines()[0],
                "reason": "Meaning needs a clearer term.",
                "expected_correction": "Use the source term exactly.",
            }
        ]
    )
    executor = FifoModels(
        [
            json.dumps({"files": {"en/a.md": corrected["en/a.md"].decode()}}),
            json.dumps({"files": {"en/b.md": corrected["en/b.md"].decode()}}),
            json.dumps({"verdict": verdict, "findings": findings}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )
    events = []

    def validate(files: Mapping[str, bytes]):
        assert set(files) <= set(corrected)
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
    assert events == ["call", "validated", "call", "validated", "call", "call"]
    assert [call.role for call in executor.calls] == [
        ModelRole.CRITIC,
        ModelRole.CRITIC,
        ModelRole.ARBITER,
        ModelRole.ARBITER,
    ]
    assert [call.model for call in executor.calls] == ["editor", "editor", "judge", "judge"]
    assert prompt_map(executor.calls[0], "translation-pr-files") == {
        "en/a.md": original["en/a.md"].decode()
    }
    assert prompt_map(executor.calls[1], "translation-pr-files") == {
        "en/b.md": original["en/b.md"].decode()
    }
    assert prompt_map(executor.calls[2], "translation-pr-files") == {
        "en/a.md": corrected["en/a.md"].decode()
    }
    assert executor.responses == []


def test_invalid_files_retry_then_marks_unreviewed_red():
    """REQUIREMENTS §4.1: invalid critic bytes after retry → RED, not arbiter on draft."""
    original = {"en/a.md": b"# Before\n"}
    executor = FifoModels(
        [
            '{"files":{"en/a.md":"# After\\n"}}',
            '{"files":{"en/a.md":"# After\\n"}}',
            '{"verdict":"GREEN","findings":[]}',
        ]
    )

    def reject(_files):
        raise quality.QualityInputError("protected")

    corrected, result = quality.review_pr(
        executor,
        critic_model="editor",
        arbiter_model="judge",
        source_files={"ru/a.md": b"# Source\n"},
        translated_files=original,
        glossary_files={},
        validate_files=reject,
    )
    assert corrected == original
    assert result.verdict.value == "RED"
    assert [call.role.value for call in executor.calls] == ["critic", "critic"]


@pytest.mark.parametrize(
    "response",
    [
        '{"verdict":"RED","findings":[]}',
        ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
    ],
)
def test_critic_failure_retries_once_then_marks_unreviewed_red(response):
    executor = FifoModels([response, response, '{"verdict":"GREEN","findings":[]}'])
    corrected, result = quality.review_pr(
        executor,
        critic_model="editor",
        arbiter_model="judge",
        source_files={"ru/a.md": b"# Source\n"},
        translated_files={"en/a.md": b"# Before\n"},
        glossary_files={},
        validate_files=lambda files: None,
    )
    assert corrected == {"en/a.md": b"# Before\n"}
    assert result.verdict.value == "RED"
    assert [call.role.value for call in executor.calls] == ["critic", "critic"]


def test_fitting_pairs_still_split_one_per_chunk_and_keep_fixture_glossary():
    full = "# BlobDepot\n\n" + "Complete paragraph.\n\n" * 200
    glossary = ("Term definition.\n" * 20) + "Glossary tail.\n"
    executor = FifoModels(
        [
            json.dumps({"files": {"en/a.md": full}}),
            json.dumps({"files": {"en/b.md": full}}),
            '{"verdict":"GREEN","findings":[]}',
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
    assert len(executor.calls) == 4
    assert [call.role for call in executor.calls] == [
        ModelRole.CRITIC,
        ModelRole.CRITIC,
        ModelRole.ARBITER,
        ModelRole.ARBITER,
    ]
    assert prompt_map(executor.calls[0], "source-pr-files") == {"ru/a.md": full}
    assert prompt_map(executor.calls[1], "source-pr-files") == {"ru/b.md": full}
    for call in executor.calls:
        assert prompt_map(call, "project-glossary") == {"glossary.md": glossary}
