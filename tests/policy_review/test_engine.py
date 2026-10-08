from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tests.policy_review.helpers import PATH, response, semantic_json, snapshot, snapshot_json
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import HttpRequest, HttpResponse, ModelRequest, YandexCredentials
from ydbdoc_review_ng.models.configuration import PRODUCTION_MODEL
from ydbdoc_review_ng.policy_review.budget import RunBudget
from ydbdoc_review_ng.policy_review.engine import _validated_result, review_snapshot
from ydbdoc_review_ng.policy_review.formal import formal_findings, new_findings
from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
from ydbdoc_review_ng.policy_review.policy import PolicyBundle
from ydbdoc_review_ng.policy_review.types import RULE_ROOT, ReviewError, ReviewFile, ReviewSnapshot

pytestmark = pytest.mark.unit


def test_reads_complete_normative_sources_in_canonical_order() -> None:
    bundle = PolicyBundle.load(snapshot())
    assert [name for name, _ in bundle.files] == [
        RULE_ROOT + name for name in (
            "DOCUMENTATION_POLICY.md", "GENERAL_RULES.md", "FORMAT_RULES.md",
            "DOCUMENTATION_RULES.md"
        )
    ]
    changed = snapshot_json()
    changed["rules"][RULE_ROOT + "FORMAT_RULES.md"] += "\nAdditional normative text"  # type: ignore[index]
    assert PolicyBundle.load(ReviewSnapshot.from_json(changed)).digest != bundle.digest


def test_missing_rule_fails_before_model_call() -> None:
    value = snapshot_json()
    del value["rules"][RULE_ROOT + "GENERAL_RULES.md"]  # type: ignore[attr-defined]
    with pytest.raises(ReviewError, match="normative_rules_missing"):
        review_snapshot(ReviewSnapshot.from_json(value))


@pytest.mark.parametrize("path", ["../secret", "ydb/docs/../secret", "/ydb/docs/page.md",
                                  "ydb/docs//page.md", "ydb/docs/a\\b.md", "ydb/docs/a\n.md"])
def test_paths_cannot_escape_documentation_scope(path: str) -> None:
    value = snapshot_json()
    value["files"][0]["path"] = path  # type: ignore[index]
    with pytest.raises(ReviewError, match="invalid_documentation_path"):
        ReviewSnapshot.from_json(value)


def test_formal_checks_find_changes_and_skip_code_list_contents() -> None:
    body = "# Title\n\nText\n- list\n\n```sql\n- this is code\n```\n\nNew text \n"
    file = ReviewFile(PATH, None, body)
    found = formal_findings(file)
    assert {(f.rule_id, f.line) for f in found} == {
        ("FORMAT.MD032", 4), ("FORMAT.YQL", 6), ("FORMAT.MD009", 10)
    }
    assert all(f.quote == body.splitlines()[f.line - 1] for f in found)
    assert new_findings(ReviewFile(PATH, body, body), found) == ()


def test_allowed_hard_break_and_fenced_example_of_sql_do_not_trigger_false_positive() -> None:
    file = ReviewFile(PATH, None, "Text  \n\n````markdown\n```sql\nselect 1;\n```\n````\n")
    assert formal_findings(file) == ()


def test_yaml_frontmatter_list_is_not_a_markdown_list() -> None:
    file = ReviewFile(PATH, None, "---\ntitle: Test\nkeywords:\n- data\n---\n\n# Test\n")
    assert formal_findings(file) == ()


def test_formal_only_is_explicitly_incomplete_and_costs_nothing() -> None:
    report = review_snapshot(snapshot())
    assert report.status == "formal_only" and report.cost_rub == "0"
    assert any(c["rule_id"] == "DOC.1" and c["status"] == "not_checked" for c in report.coverage)
    assert "Описание" not in repr(snapshot())


@pytest.mark.parametrize("field,value", [("quote", "Invented"), ("path", "ydb/docs/ru/fake.md"),
                                         ("line", 99), ("line", True)])
def test_model_cannot_publish_invented_location_or_quote(field: str, value: object) -> None:
    finding = {"rule_id": "DOC.1", "path": PATH, "line": 1, "quote": "# Test",
               "explanation": "Нужно определение.", "suggestion": "Добавьте определение."}
    finding[field] = value
    with pytest.raises(ReviewError):
        _validated_result(semantic_json(findings=[finding]), snapshot().files)


def test_coverage_requires_every_rule_and_cannot_claim_visual_style_was_checked() -> None:
    value = json.loads(semantic_json())
    value["coverage"].pop()
    with pytest.raises(ReviewError, match="incomplete_model_coverage"):
        _validated_result(json.dumps(value), snapshot().files)
    value = json.loads(semantic_json())
    value["coverage"][12]["status"] = "checked"
    with pytest.raises(ReviewError, match="unsupported_visual_check"):
        _validated_result(json.dumps(value), snapshot().files)


def test_each_retry_reserves_and_audits_money_without_hidden_provider_retry() -> None:
    requests: list[HttpRequest] = []
    responses = iter([response("not JSON"), response(semantic_json())])
    def transport(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return next(responses)
    events = []
    attempts = []
    model = BudgetedPolicyModel(YandexCredentials("test-only", "folder"), transport,
                                RunBudget(Decimal(50), events.append), attempts.append)
    report = review_snapshot(snapshot(), model=model)
    assert report.status == "incomplete"  # Links are explicitly not implemented in this stage.
    assert len(requests) == 2 and len(attempts) == 2 and report.cost_rub == "0.2"
    assert [e.kind for e in events] == ["reserved", "settled", "reserved", "settled"]
    payload = json.loads(requests[0].body)
    assert payload["model"].endswith(PRODUCTION_MODEL) and payload["reasoning_effort"] == "none"
    assert payload["max_tokens"] == 4096 and len(payload["messages"]) == 2


def test_missing_cost_denies_retry_and_does_not_become_zero() -> None:
    requests = []
    def transport(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(200, b'{}')
    model = BudgetedPolicyModel(YandexCredentials("test-only", "folder"), transport,
                                RunBudget(Decimal(50), lambda _: None), lambda _: None)
    report = review_snapshot(snapshot(), model=model)
    assert len(requests) == 1 and report.cost_rub is None and report.status == "cost_unknown"
    assert Decimal(report.reserved_rub) > 0


def test_zero_budget_never_calls_transport() -> None:
    def transport(request: HttpRequest) -> HttpResponse:
        pytest.fail("No paid call should be started")
    model = BudgetedPolicyModel(YandexCredentials("test-only", "folder"), transport,
                                RunBudget(Decimal(0), lambda _: None), lambda _: None)
    report = review_snapshot(snapshot(), model=model)
    assert report.status == "budget_exceeded" and report.cost_rub == "0"


def test_unexpected_transport_interruption_retains_billable_reserve() -> None:
    def transport(request: HttpRequest) -> HttpResponse:
        raise RuntimeError("secret transport error")
    budget = RunBudget(Decimal(50), lambda _: None)
    model = BudgetedPolicyModel(YandexCredentials("test-only", "folder"), transport,
                                budget, lambda _: None)
    with pytest.raises(ReviewError, match="^model_attempt_interrupted$"):
        model.invoke(ModelRequest(ModelRole.ARBITER, PRODUCTION_MODEL, "check", None,
                                  max_output_tokens=4096))
    assert budget.unknown and budget.reserved > 0


def test_retry_cannot_spend_past_run_limit() -> None:
    requests: list[HttpRequest] = []
    def transport(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return response("malformed JSON", Decimal(49))
    model = BudgetedPolicyModel(YandexCredentials("test-only", "folder"), transport,
                                RunBudget(Decimal(50), lambda _: None), lambda _: None)
    report = review_snapshot(snapshot(), model=model)
    assert len(requests) == 1 and report.status == "budget_insufficient"
    assert report.cost_rub == "49"


def test_billable_provider_overrun_stops_before_accepting_a_clean_verdict() -> None:
    calls: list[HttpRequest] = []
    def transport(request: HttpRequest) -> HttpResponse:
        calls.append(request)
        return response(semantic_json(), Decimal(51))
    model = BudgetedPolicyModel(YandexCredentials("test-only", "folder"), transport,
                                RunBudget(Decimal(50), lambda _: None), lambda _: None)
    report = review_snapshot(snapshot(), model=model)
    assert len(calls) == 1 and report.status == "budget_exceeded" and report.cost_rub == "51"


def test_attempt_audit_failure_prevents_following_model_call() -> None:
    def transport(request: HttpRequest) -> HttpResponse:
        return response(semantic_json())
    def fail_audit(attempt: object) -> None:
        raise RuntimeError("secret diagnostics")
    model = BudgetedPolicyModel(YandexCredentials("test-only", "folder"), transport,
                                RunBudget(Decimal(50), lambda _: None), fail_audit)
    request = ModelRequest(ModelRole.ARBITER, PRODUCTION_MODEL, "check", None,
                           max_output_tokens=4096)
    with pytest.raises(ReviewError, match="^model_attempt_interrupted$"):
        model.invoke(request)
    assert model.invoke(request).status == "model_audit_unavailable"


def test_image_style_cannot_be_marked_not_applicable_when_image_is_present() -> None:
    with pytest.raises(ReviewError, match="unsupported_visual_check"):
        _validated_result(semantic_json(), snapshot(after="# Test\n\n![Image](test.png)\n").files)


def test_real_policy_fixture_is_complete_and_produces_grounded_offline_findings() -> None:
    root = Path(__file__).parents[1] / "fixtures" / "policy_review" / "snapshot.json"
    frozen = ReviewSnapshot.from_json(json.loads(root.read_text()))
    report = review_snapshot(frozen)
    assert report.status == "formal_only" and report.rules_sha == frozen.rules_sha
    assert {(f.rule_id, f.line) for f in report.findings} == {
        ("FORMAT.MD032", 4), ("FORMAT.YQL", 6), ("FORMAT.MD009", 10)
    }
