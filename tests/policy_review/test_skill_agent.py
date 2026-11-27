import json

import pytest

from ydbdoc_review_ng.policy_review.agent import review_pr
from ydbdoc_review_ng.policy_review.github import ReviewGitHub
from ydbdoc_review_ng.policy_review.types import ReviewError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["changelog-server.md", "release-notes/v26.md", "version-history.md"])
def test_history_tool_returns_only_changed_lines_without_fetching_full_file(name):
    from ydbdoc_review_ng.policy_review.agent import public_changes, read_source
    path = "ydb/docs/ru/core/" + name
    changes = [{"filename": path, "patch": "@@ -900,2 +900,2 @@\n-old\n+new\n unrelated record"}]
    assert "unrelated record" not in public_changes(changes)[0]["patch"]
    github = ReviewGitHub(lambda *_: pytest.fail("requested full history"))
    args = {"path": path, "revision": "head", "start_line": 900, "end_line": 900}
    result = read_source(github, changes, "a" * 40, "b" * 40, "read_file", args)
    assert "new" in json.dumps(result)
    args["start_line"] = 1
    with pytest.raises(ReviewError):
        read_source(github, changes, "a" * 40, "b" * 40, "read_file", args)


def test_new_version_page_in_releases_stays_out_of_model_context_and_file_reads():
    from ydbdoc_review_ng.policy_review.agent import public_changes, read_source
    path = "ydb/docs/en/core/releases/26.4.md"
    changes = [{"filename": path, "status": "added", "patch": "@@ -0,0 +1 @@\n+Release history"}]
    assert "patch" not in public_changes(changes)[0]
    github = ReviewGitHub(lambda *_: pytest.fail("requested historical file"))
    with pytest.raises(ReviewError, match="historical_read_outside_changed_entry"):
        read_source(github, changes, "a" * 40, "b" * 40, "read_file",
                    {"path": path, "revision": "head", "start_line": 1, "end_line": 1})


def tools_reply(*calls):
    from ydbdoc_review_ng.models import HttpResponse
    return HttpResponse(200, json.dumps({
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"type": "function", "id": f"call-{i}-{name}",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(calls)]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }).encode())


@pytest.mark.parametrize("plain_markdown", [False, True])
def test_native_provider_can_read_head_then_complete_report(plain_markdown):
    import base64
    from decimal import Decimal

    from ydbdoc_review_ng.models import YandexCredentials
    from ydbdoc_review_ng.policy_review.agent import SKILL
    from ydbdoc_review_ng.policy_review.budget import RunBudget
    from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
    from ydbdoc_review_ng.policy_review.types import ReviewDiff

    path = "ydb/docs/en/core/contributor/release-process.md"
    body = b"# How to publish a release\n1. Build the archive.\n"
    diff = ReviewDiff("a" * 40, "b" * 40, "c" * 40, {"pr": 42}, [
        {"filename": path, "status": "added",
         "patch": "@@ -0,0 +1,2 @@\n+# How to publish a release\n+1. Build the archive."}])
    def api(method, url, payload):
        assert method == "GET" and url.endswith(f"/contents/{path}?ref={diff.head_sha}")
        return {"type": "file", "encoding": "base64", "size": len(body),
                "content": base64.b64encode(body).decode()}

    wires = []
    events = []
    def transport(request):
        wires.append(json.loads(request.body))
        assert events[-1].kind == "reserved"
        if len(wires) == 1:
            return tools_reply(("read_file", {"path": path, "revision": "head",
                                              "start_line": 2, "end_line": 2}))
        if plain_markdown:
            from ydbdoc_review_ng.models import HttpResponse
            return HttpResponse(200, json.dumps({
                "choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": "Changed wording. Preview unavailable."}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }).encode())
        return tools_reply(("finish_review", {"summary": "Changed wording.", "checked": "Line 1.",
                           "gaps": ["Preview unavailable."], "findings": []}))
    model = BudgetedPolicyModel(YandexCredentials("private-api-key", "test"), transport,
                                RunBudget(Decimal(100), events.append), lambda _: None)
    report = review_pr(diff, ReviewGitHub(api), model)
    assert len(wires) == 2 and all(w["max_tokens"] == 8192 for w in wires)
    assert all(w["tool_choice"] == "auto" for w in wires)
    assert all(w["reasoning_effort"] == "medium" for w in wires)
    assert wires[0]["messages"][0]["role"] == "system"
    assert wires[0]["messages"][0]["content"] == (SKILL / "SKILL.md").read_text()
    assert "+1. Build the archive." in wires[0]["messages"][1]["content"]
    assert json.loads(wires[1]["messages"][-1]["content"])["lines"] == [[2, "1. Build the archive."]]
    assert "private-api-key" not in json.dumps(wires)
    assert [e.kind for e in events] == ["reserved", "settled"] * 2
    assert "Changed wording." in report["markdown"] and "Preview unavailable." in report["markdown"]
    if plain_markdown:
        assert report["status"] != "clean"


@pytest.mark.parametrize("limit", ["0", "0.01"])
def test_insufficient_budget_before_agent_turn_never_contacts_provider(limit):
    from decimal import Decimal

    from ydbdoc_review_ng.models import YandexCredentials
    from ydbdoc_review_ng.policy_review.budget import RunBudget
    from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
    from ydbdoc_review_ng.policy_review.types import ReviewDiff

    diff = ReviewDiff("a" * 40, "b" * 40, "c" * 40, {}, [])
    model = BudgetedPolicyModel(YandexCredentials("fake", "test"), lambda _: pytest.fail("paid"),
                                RunBudget(Decimal(limit), lambda _: pytest.fail("reserved")), lambda _: None)
    with pytest.raises(ReviewError, match="budget_(exceeded|insufficient)"):
        review_pr(diff, ReviewGitHub(lambda *_: pytest.fail("unexpected GitHub read")), model)


def test_identical_failed_tool_stops_after_one_correction_attempt(capsys):
    from decimal import Decimal

    from ydbdoc_review_ng.models import YandexCredentials
    from ydbdoc_review_ng.policy_review.budget import RunBudget
    from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
    from ydbdoc_review_ng.policy_review.types import ReviewDiff

    wires, events = [], []
    def transport(request):
        wires.append(json.loads(request.body))
        if len(wires) == 2:
            assert json.loads(wires[-1]["messages"][-1]["content"])["error"] == "invalid_line_window"
        return tools_reply(("read_file", {
            "path": "ydb/docs/en/topic.md", "revision": "head", "start_line": 1,
            "end_line": 200, "literal": "DO_NOT_LOG_ARGUMENT_CONTENT",
        }))

    model = BudgetedPolicyModel(YandexCredentials("private-key", "test"), transport,
                               RunBudget(Decimal(100), events.append), lambda _: None)
    with pytest.raises(ReviewError, match="repeated_tool_failure"):
        review_pr(ReviewDiff("a" * 40, "b" * 40, "c" * 40, {}, []),
                  ReviewGitHub(lambda *_: pytest.fail("invalid window must not fetch a file")), model)
    assert len(wires) == 2
    assert [e.kind for e in events] == ["reserved", "settled"] * 2
    output = capsys.readouterr().out
    assert "invalid_line_window" in output
    assert "DO_NOT_LOG_ARGUMENT_CONTENT" not in output and "private-key" not in output


def test_identical_successful_read_cannot_repeat_forever():
    from decimal import Decimal

    from ydbdoc_review_ng.models import YandexCredentials
    from ydbdoc_review_ng.policy_review.budget import RunBudget
    from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
    from ydbdoc_review_ng.policy_review.types import ReviewDiff

    requests = []
    def transport(request):
        requests.append(request)
        return tools_reply(("read_resource", {"name": "placement"}))
    model = BudgetedPolicyModel(YandexCredentials("test", "test"), transport,
                               RunBudget(Decimal(100), lambda _: None), lambda _: None)
    with pytest.raises(ReviewError, match="repeated_tool_call"):
        review_pr(ReviewDiff("a" * 40, "b" * 40, "c" * 40, {}, []),
                  ReviewGitHub(lambda *_: pytest.fail("local resource needs no network")), model)
    assert len(requests) == 3


def test_report_links_current_hunk_and_rejects_fabricated_anchor():
    from ydbdoc_review_ng.policy_review.agent import finish
    from ydbdoc_review_ng.policy_review.types import ReviewDiff
    path = "ydb/docs/en/topic.md"
    diff = ReviewDiff("a" * 40, "b" * 40, "c" * 40, {}, [
        {"filename": path, "patch": "@@ -12 +12 @@\n-old\n+typoo"}])
    finding = {"path": path, "line": 12, "problem": "Typo.", "proof": "`typoo`", "correction": "`typo`"}
    args = {"summary": "One typo.", "checked": "The changed line.", "findings": [finding], "gaps": []}
    report = finish(diff, args, [])
    assert f"/blob/{diff.head_sha}/{path}#L12" in report["markdown"]
    assert "Исправление: `typo`" in report["markdown"]
    finding["line"] = 11
    with pytest.raises(ReviewError, match="outside_head_hunk"):
        finish(diff, args, [])


@pytest.mark.parametrize(("bound", "reason"), [("MAX_TURNS", "review_turn_limit"),
                                              ("MAX_CALLS", "review_tool_limit")])
def test_agent_stops_at_step_limits(monkeypatch, bound, reason):
    from decimal import Decimal

    from ydbdoc_review_ng.models import YandexCredentials
    from ydbdoc_review_ng.policy_review import agent
    from ydbdoc_review_ng.policy_review.budget import RunBudget
    from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
    from ydbdoc_review_ng.policy_review.types import ReviewDiff

    monkeypatch.setattr(agent, bound, 1)
    calls = []
    def transport(request):
        calls.append(request)
        return tools_reply(("read_resource", {"name": "placement"}),
                           ("read_resource", {"name": "public-map"}))
    model = BudgetedPolicyModel(YandexCredentials("test", "test"), transport,
                                RunBudget(Decimal(100), lambda _: None), lambda _: None)
    with pytest.raises(ReviewError, match=reason):
        review_pr(ReviewDiff("a" * 40, "b" * 40, "c" * 40, {}, []),
                  ReviewGitHub(lambda *_: pytest.fail("unexpected source read")), model)
    assert len(calls) == 1


def test_agent_finishes_before_reading_uses_remaining_report_budget():
    from decimal import Decimal

    from ydbdoc_review_ng.models import HttpResponse, YandexCredentials
    from ydbdoc_review_ng.policy_review.budget import RunBudget
    from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
    from ydbdoc_review_ng.policy_review.types import ReviewDiff

    wires, events = [], []
    budget = RunBudget(Decimal(100), events.append)

    def transport(request):
        wire = json.loads(request.body)
        wires.append(wire)
        if len(wires) == 1:
            # Previous audited calls left enough for a report, but not safe further exploration.
            budget.spent = Decimal(80)
            return tools_reply(("read_resource", {"name": "placement"}))
        assert not wire.get("tools")
        assert wire["messages"][-1]["role"] == "user"
        assert "final" in wire["messages"][-1]["content"].lower()
        return HttpResponse(200, json.dumps({
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "Reviewed available diff. Preview was not checked."}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }).encode())

    model = BudgetedPolicyModel(YandexCredentials("test", "test"), transport, budget, lambda _: None)
    result = review_pr(ReviewDiff("a" * 40, "b" * 40, "c" * 40, {}, []),
                       ReviewGitHub(lambda *_: pytest.fail("no remote reads")), model)
    assert len(wires) == 2
    assert result["status"] == "incomplete"
    assert "Reviewed available diff" in result["markdown"]
    assert budget.spent < budget.limit


def test_empty_historical_search_is_explicitly_limited_to_changed_lines():
    from ydbdoc_review_ng.policy_review.agent import read_source

    path = "ydb/docs/ru/core/changelog-server.md"
    changes = [{"filename": path, "patch": "@@ -90 +90 @@\n-логгировать\n+логировать"}]
    result = read_source(ReviewGitHub(lambda *_: pytest.fail("must not fetch history")),
                         changes, "a" * 40, "b" * 40, "search",
                         {"path": path, "revision": "head", "literal": "логгир"})
    assert result["lines"] == []
    assert result["scope"] == "changed_lines_only"
    assert "entire file" in result["coverage_notice"]


def test_regular_search_reports_scope_and_truncation():
    import base64

    from ydbdoc_review_ng.policy_review.agent import read_source

    body = ("match\n" * 21).encode()
    github = ReviewGitHub(lambda *_: {"type": "file", "encoding": "base64", "size": len(body),
                                     "content": base64.b64encode(body).decode()})
    result = read_source(github, [], "a" * 40, "b" * 40, "search",
                         {"path": "ydb/docs/en/topic.md", "revision": "head", "literal": "match"})
    assert result["scope"] == "entire_file"
    assert result["truncated"] is True
    assert len(result["lines"]) == 20


def test_thinking_tool_round_preserves_opaque_provider_state_without_logging(capsys):
    from decimal import Decimal

    from ydbdoc_review_ng.models import HttpResponse, YandexCredentials
    from ydbdoc_review_ng.policy_review.budget import RunBudget
    from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
    from ydbdoc_review_ng.policy_review.types import ReviewDiff

    wires = []
    opaque = "opaque-provider-state-never-publish"

    def transport(request):
        wire = json.loads(request.body)
        wires.append(wire)
        assert wire["reasoning_effort"] == "medium"
        if len(wires) == 1:
            response = tools_reply(("read_resource", {"name": "placement"}))
            body = json.loads(response.body)
            body["choices"][0]["message"]["reasoning_content"] = opaque
            return HttpResponse(200, json.dumps(body).encode())
        assistant = [m for m in wire["messages"] if m["role"] == "assistant"][-1]
        assert assistant["reasoning_content"] == opaque
        return HttpResponse(200, json.dumps({
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "Review complete; unavailable checks named."}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }).encode())

    model = BudgetedPolicyModel(YandexCredentials("test", "test"), transport,
                               RunBudget(Decimal(100), lambda _: None), lambda _: None)
    result = review_pr(ReviewDiff("a" * 40, "b" * 40, "c" * 40, {}, []),
                       ReviewGitHub(lambda *_: pytest.fail("no remote source")), model)
    assert len(wires) == 2
    assert opaque not in json.dumps(result)
    assert opaque not in capsys.readouterr().out
