import json

import pytest

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.models.types import ModelCallResult
from ydbdoc_review_ng.translation.partial import (
    PartialUpdateError,
    apply_replacements,
    translate_partial,
)


def response(edits):
    return json.dumps({"replacements": edits})


@pytest.mark.parametrize(
    "before,after,target,edits,expected",
    [
        (
            "Base.\n",
            "Base.\n\nA.\n\nB.\n",
            b"Base EN.\r\n\r\nA EN.\r\n",
            [{"old": "A EN.\r\n", "new": "A EN.\r\n\r\nB EN.\r\n"}],
            b"Base EN.\r\n\r\nA EN.\r\n\r\nB EN.\r\n",
        ),
        (
            "Base.\n\nA.\n\nB.\n",
            "Base.\n",
            b"Base EN.\n\nB EN.\n",
            [{"old": "\nB EN.\n", "new": ""}],
            b"Base EN.\n",
        ),
    ],
)
def test_partial_changes_use_current_target_and_preserve_other_bytes(
    before, after, target, edits, expected
):
    calls = []

    def invoke(request):
        calls.append(request)
        return ModelCallResult(response(edits), None, ())

    actual = translate_partial(
        before.encode(),
        after.encode(),
        target,
        source_locale="ru",
        target_path=RepoPath("en/page.md"),
        model="model",
        invoke=invoke,
    )
    assert actual == expected
    assert len(calls) == 1
    assert (
        target.decode() in json.loads(calls[0].prompt.split("Evidence: ", 1)[1])["current_target"]
    )
    assert calls[0].max_output_tokens == 16384


@pytest.mark.parametrize(
    "edits",
    [
        [{"old": "", "new": "x"}],
        [{"old": "same", "new": "x"}],
        [{"old": "absent", "new": "x"}],
        [{"old": "same same", "new": "x"}, {"old": "same end", "new": "y"}],
        [{"old": "end", "new": "x", "extra": True}],
    ],
)
def test_reject_ambiguous_missing_overlapping_or_malformed_replacements(edits):
    with pytest.raises(PartialUpdateError):
        apply_replacements(b"start same same end\n", response(edits))


def test_reject_duplicate_json_fields_and_full_document_rewrite():
    with pytest.raises(PartialUpdateError):
        apply_replacements(b"abc", '{"replacements":[],"replacements":[]}')
    with pytest.raises(PartialUpdateError):
        apply_replacements(b"abc", response([{"old": "abc", "new": "entire rewrite"}]))


def test_reject_collective_whole_document_rewrite():
    target = b"First paragraph.\n\nSecond paragraph.\n"
    edits = [
        {"old": "First paragraph.\n\n", "new": "Entirely unrelated rewrite. "},
        {"old": "Second paragraph.\n", "new": "Removed original document.\n"},
    ]
    with pytest.raises(PartialUpdateError, match="whole_document_rewrite"):
        apply_replacements(target, response(edits))


def test_invalid_patch_retries_once_and_never_falls_back():
    calls = []

    def invoke(request):
        calls.append(request)
        return ModelCallResult(response([{"old": "ambiguous", "new": "x"}]), None, ())

    with pytest.raises(PartialUpdateError):
        translate_partial(
            b"old",
            b"new",
            b"ambiguous ambiguous",
            source_locale="ru",
            target_path=RepoPath("en/page.md"),
            model="model",
            invoke=invoke,
        )
    assert len(calls) == 2


def test_oversize_evidence_fails_without_model_call():
    def forbidden(request):
        raise AssertionError("model must not receive truncated evidence")

    with pytest.raises(PartialUpdateError):
        translate_partial(
            b"old",
            b"x" * 200000,
            b"target",
            source_locale="ru",
            target_path=RepoPath("en/page.md"),
            model="model",
            invoke=forbidden,
        )


def test_runtime_routes_selected_bilingual_pair_around_legacy_surgical(monkeypatch):
    from tests.unit.test_pair_runtime import runtime
    from ydbdoc_review_ng import runtime_content
    from ydbdoc_review_ng.translation import partial

    content, preparation = runtime([("a.md", "ru_to_en", "Only second claim missing")])
    plans = content.select_source(preparation)
    calls = []

    def patch(*args, **kwargs):
        calls.append(args)
        return b"# Title\n\nAlready Translated.\n\nNew Claim.\n"

    monkeypatch.setattr(partial, "translate_partial", patch)
    monkeypatch.setattr(
        runtime_content, "plan_surgical_update", lambda *args: pytest.fail("legacy surgical route")
    )
    _, accepted = content._translate_document(plans.documents[0])
    assert len(calls) == 1
    assert accepted.translated_markdown == "# Title\n\nAlready Translated.\n\nNew Claim.\n"


def test_runtime_partial_failure_does_not_use_legacy_or_full_translation(monkeypatch):
    from tests.unit.test_pair_runtime import runtime
    from ydbdoc_review_ng import runtime_content
    from ydbdoc_review_ng.translation import partial

    content, preparation = runtime([("a.md", "ru_to_en", "Only second claim missing")])
    plans = content.select_source(preparation)

    def fail(*args, **kwargs):
        raise PartialUpdateError("ambiguous")

    monkeypatch.setattr(partial, "translate_partial", fail)
    monkeypatch.setattr(
        runtime_content, "plan_surgical_update", lambda *args: pytest.fail("legacy surgical route")
    )
    with pytest.raises(
        runtime_content.InvalidTranslationResponse, match="partial_translation_patch_invalid"
    ):
        content._translate_document(plans.documents[0])


def test_retry_repairs_invalid_patch_and_preserves_unicode_outside_changes():
    responses = iter(
        [
            response([{"old": "absent", "new": "x"}]),
            response([{"old": "Old.", "new": "New."}]),
        ]
    )

    def invoke(request):
        return ModelCallResult(next(responses), None, ())

    target = "Начало.\r\n\r\nOld.\n\nКонец.\r\n".encode()
    result = translate_partial(
        b"Old.",
        b"New.",
        target,
        source_locale="en",
        target_path=RepoPath("ru/page.md"),
        model="model",
        invoke=invoke,
    )
    assert result == target.replace(b"Old.", b"New.")
