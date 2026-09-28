"""Provider-filter recovery must never silently skip unchecked excerpts."""
from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng.domain import Locale, ModelRole, RepoPath
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.quality import QualityExecutionError, Verdict
from ydbdoc_review_ng.quality.repair import _invoke_critic


def excerpt(request: ModelRequest, tag: str) -> str:
    return request.prompt.split(f"<{tag}>\n", 1)[1].split(f"</{tag}>", 1)[0]


@pytest.mark.parametrize("final", [False, True])
def test_readonly_filter_recovers_all_excerpts_without_replaying_accepted(final: bool) -> None:
    source = "\n\n".join(f"Source paragraph {n:02d}." for n in range(16)).encode()
    target = source.replace(b"Source", b"Target")
    calls: list[ModelRequest] = []
    accepted: list[ModelRequest] = []
    hooks: list[None] = []

    class Executor:
        def invoke(self, request: ModelRequest, /) -> ModelCallResult:
            calls.append(request)
            if len(calls) in {1, 3}:
                return ModelCallResult(None, AttemptError.CONTENT_FILTER, ())
            accepted.append(request)
            return ModelCallResult(json.dumps({"verdict": "GREEN", "findings": []}), None, ())

    result = _invoke_critic(
        Executor(), model="model", source=source, target=target,
        target_path=RepoPath("ydb/docs/en/example.md"), source_locale=Locale.RU,
        target_locale=Locale.EN, requested_ids=(), final=final,
        before_model_call=lambda: hooks.append(None),
    )
    assert result.verdict is Verdict.GREEN
    assert len(calls) == len(hooks) == 5
    assert {call.role for call in calls} == {
        ModelRole.FINAL_CRITIC if final else ModelRole.CRITIC
    }
    assert "".join(excerpt(r, "authoritative-source") for r in accepted).encode() == source
    assert "".join(excerpt(r, "final-target") for r in accepted).encode() == target
    assert all("corresponding ordered excerpts" in r.prompt for r in accepted)


@pytest.mark.parametrize("failure, expected_calls", [
    (AttemptError.CONTENT_FILTER, 5), (AttemptError.TRANSPORT, 1),
])
@pytest.mark.parametrize("final", [False, True])
def test_readonly_filter_is_bounded_and_never_green_on_failure(
    failure: AttemptError, expected_calls: int, final: bool,
) -> None:
    calls: list[ModelRequest] = []

    class Executor:
        def invoke(self, request: ModelRequest, /) -> ModelCallResult:
            calls.append(request)
            return ModelCallResult(None, failure, ())

    with pytest.raises(QualityExecutionError):
        _invoke_critic(
            Executor(), model="model", source=b"paragraph\n\n" * 128,
            target=b"translated\n\n" * 128,
            target_path=RepoPath("ydb/docs/en/example.md"), source_locale=Locale.RU,
            target_locale=Locale.EN, requested_ids=(), final=final,
        )
    assert len(calls) == expected_calls
