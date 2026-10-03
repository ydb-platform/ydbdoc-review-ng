from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from tests.support.scripted_models import ScriptedModels
from ydbdoc_review_ng import quality
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import AttemptError, ModelCallResult


class FifoModels(ScriptedModels):
    @property
    def responses(self):
        return self.payloads


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
    critic_turns_per_file = 1 if not changed else 3
    expected_events = (
        (["call"] * critic_turns_per_file + ["validated"]) * 2 + ["call", "call"]
    )
    assert events == expected_events
    critic_calls = [call for call in executor.calls if call.role is ModelRole.CRITIC]
    arbiter_calls = [call for call in executor.calls if call.role is ModelRole.ARBITER]
    assert len(critic_calls) == 2 * critic_turns_per_file
    assert len(arbiter_calls) == 2
    assert {call.model for call in critic_calls} == {"editor"}
    assert {call.model for call in arbiter_calls} == {"judge"}
    assert prompt_map(critic_calls[0], "translation-pr-files") == {
        "en/a.md": original["en/a.md"].decode()
    }
    # First turn of second file chunk.
    assert prompt_map(critic_calls[critic_turns_per_file], "translation-pr-files") == {
        "en/b.md": original["en/b.md"].decode()
    }
    assert prompt_map(arbiter_calls[0], "translation-pr-files") == {
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
    assert all(call.role.value == "critic" for call in executor.calls)
    assert len(executor.calls) == 6  # two sessions × patch/read/finish, validate fails


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
    critic_calls = [call for call in executor.calls if call.role is ModelRole.CRITIC]
    arbiter_calls = [call for call in executor.calls if call.role is ModelRole.ARBITER]
    assert len(critic_calls) == 2  # finish-only; draft already matches
    assert len(arbiter_calls) == 2
    assert prompt_map(critic_calls[0], "source-pr-files") == {"ru/a.md": full}
    assert prompt_map(critic_calls[1], "source-pr-files") == {"ru/b.md": full}
    for call in executor.calls:
        assert prompt_map(call, "project-glossary") == {"glossary.md": glossary}


def test_model_exempt_targets_are_python_reviewed_green_without_model() -> None:
    class Boom:
        def invoke(self, request):
            raise AssertionError(f"model must not run: {request.role}")

    corrected, result = quality.review_pr(
        Boom(),
        critic_model="editor",
        arbiter_model="judge",
        source_files={"ru/a.md": b"* [cfg](./new.md).\n"},
        translated_files={"en/a.md": b"* [cfg](./new.md).\n"},
        glossary_files={},
        validate_files=lambda files: None,
        model_exempt_targets=frozenset({"en/a.md"}),
    )
    assert corrected == {"en/a.md": b"* [cfg](./new.md).\n"}
    assert result.verdict is quality.Verdict.GREEN
    assert result.findings == ()


def test_mixed_pr_exempts_only_unique_replacement_target() -> None:
    original = {"en/a.md": b"# A\n", "en/b.md": b"# B\n"}
    executor = FifoModels(
        [
            json.dumps({"files": {"en/b.md": "# B reviewed\n"}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )
    corrected, result = quality.review_pr(
        executor,
        critic_model="editor",
        arbiter_model="judge",
        source_files={"ru/a.md": b"# A\n", "ru/b.md": b"# B\n"},
        translated_files=original,
        glossary_files={},
        validate_files=lambda files: None,
        model_exempt_targets=frozenset({"en/a.md"}),
    )
    assert result.verdict is quality.Verdict.GREEN
    assert corrected["en/a.md"] == b"# A\n"
    assert corrected["en/b.md"] == b"# B reviewed\n"
    critic_calls = [call for call in executor.calls if call.role is ModelRole.CRITIC]
    arbiter_calls = [call for call in executor.calls if call.role is ModelRole.ARBITER]
    assert all("en/a.md" not in (call.prompt or "") for call in critic_calls)
    assert all("en/a.md" not in (call.prompt or "") for call in arbiter_calls)
    assert arbiter_calls


def test_delta_scope_injects_brief_and_drops_historical_findings() -> None:
    from ydbdoc_review_ng.quality.delta_scope import build_pair_delta_scope

    source_before = b"* [cfg](./old.md).\n"
    source_after = b"* [cfg](./new.md).\n"
    draft = b"* [cfg](./new.md).\n* Old historical period style\n"
    scope = build_pair_delta_scope(
        source_before,
        source_after,
        draft,
        source_path="ru/a.md",
        target_path="en/a.md",
    )
    assert scope.change_class == "unique_dest"
    assert scope.restrict_findings
    executor = FifoModels(
        [
            json.dumps({"files": {"en/a.md": draft.decode()}}),
            json.dumps(
                {
                    "verdict": "YELLOW",
                    "findings": [
                        {
                            "target_path": "en/a.md",
                            "searchable_snippet": "* Old historical period style",
                            "reason": "Исторический стиль.",
                            "expected_correction": "Поставить точку в конце.",
                        }
                    ],
                }
            ),
        ]
    )
    _corrected, result = quality.review_pr(
        executor,
        critic_model="editor",
        arbiter_model="judge",
        source_files={"ru/a.md": source_after},
        translated_files={"en/a.md": draft},
        glossary_files={},
        validate_files=lambda files: None,
        delta_scopes={"en/a.md": scope},
    )
    assert result.verdict is quality.Verdict.GREEN
    assert result.findings == ()
    critic_calls = [call for call in executor.calls if call.role is ModelRole.CRITIC]
    arbiter_calls = [call for call in executor.calls if call.role is ModelRole.ARBITER]
    assert critic_calls
    assert arbiter_calls
    assert "CHANGE CLASS: unique_dest" in (critic_calls[0].prompt or "")
    assert "CHANGE CLASS: unique_dest" in (arbiter_calls[0].prompt or "")


def test_delta_scope_keeps_finding_on_touched_line() -> None:
    from ydbdoc_review_ng.quality.delta_scope import build_pair_delta_scope

    source_before = b"* [cfg](./old.md).\n"
    source_after = b"* [cfg](./new.md).\n"
    draft = b"* [cfg](./old.md).\n"
    scope = build_pair_delta_scope(
        source_before,
        source_after,
        draft,
        source_path="ru/a.md",
        target_path="en/a.md",
    )
    executor = FifoModels(
        [
            json.dumps({"files": {"en/a.md": draft.decode()}}),
            json.dumps(
                {
                    "verdict": "YELLOW",
                    "findings": [
                        {
                            "target_path": "en/a.md",
                            "searchable_snippet": "* [cfg](./old.md).",
                            "reason": "Dest не перенесён.",
                            "expected_correction": "Заменить на ./new.md.",
                        }
                    ],
                }
            ),
        ]
    )
    _corrected, result = quality.review_pr(
        executor,
        critic_model="editor",
        arbiter_model="judge",
        source_files={"ru/a.md": source_after},
        translated_files={"en/a.md": draft},
        glossary_files={},
        validate_files=lambda files: None,
        delta_scopes={"en/a.md": scope},
    )
    assert result.verdict is quality.Verdict.YELLOW
    assert len(result.findings) == 1

