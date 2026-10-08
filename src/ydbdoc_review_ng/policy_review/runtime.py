"""Model-free admission/preparation and separately credentialed paid worker."""

from __future__ import annotations

import html
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ydbdoc_review_ng.models import AttemptResult, HttpTransport, YandexCredentials
from ydbdoc_review_ng.models.configuration import PRICING_VERSION, PRODUCTION_MODEL
from ydbdoc_review_ng.policy_review.admission import ReviewTrigger, admit
from ydbdoc_review_ng.policy_review.budget import BudgetEvent, RunBudget, run_limit
from ydbdoc_review_ng.policy_review.debounce import wait_for_quiet_head
from ydbdoc_review_ng.policy_review.engine import review_snapshot
from ydbdoc_review_ng.policy_review.github import ReviewGitHub
from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
from ydbdoc_review_ng.policy_review.store import ReviewStore
from ydbdoc_review_ng.policy_review.types import ReviewError, ReviewSnapshot


@dataclass(frozen=True, slots=True)
class Preparation:
    status: str
    trigger: ReviewTrigger
    owner: str
    snapshot: ReviewSnapshot | None = None

    def plan(self) -> dict[str, object]:
        return {"status": self.status, "owner": self.owner, "trigger": asdict(self.trigger)}


def prepare_review(
    trigger: ReviewTrigger, owner: str, github: ReviewGitHub, store: ReviewStore,
    environment: Mapping[str, str], *,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> Preparation:
    status = admit(trigger, github, store)
    if status != "admitted":
        return Preparation(status, trigger, owner)
    # Files-only PRs do not consume five minutes or call the model.
    pr = github.read_pr(trigger.pr)
    files = github.files(pr)
    if not any(str(row.get("filename", "")).startswith("ydb/docs/")
               or str(row.get("previous_filename", "")).startswith("ydb/docs/") for row in files):
        return Preparation("no_reviewable_changes", trigger, owner)
    if run_limit(environment.get("YDBDOC_REVIEW_MAX_RUN_COST_RUB")) == 0:
        return Preparation("paid_review_disabled", trigger, owner)
    if not store.queue(trigger.pr, trigger.head_sha, trigger.request_id, owner):
        return Preparation("duplicate", trigger, owner)
    quiet = wait_for_quiet_head(trigger.head_sha, lambda: github.read_pr(trigger.pr).state(),
                                clock=clock, wait=wait)
    if quiet.status != "ready":
        return Preparation(quiet.status, trigger, owner)
    # Both operator permissions and author/approval are checked again after waiting.
    status = admit(trigger, github, store)
    if status != "admitted":
        return Preparation(status, trigger, owner)
    pr = github.read_pr(trigger.pr)
    if pr.head_sha != trigger.head_sha:
        return Preparation("superseded", trigger, owner)
    snapshot = github.snapshot(pr, github.files(pr), environment.get("YDBDOC_REVIEW_RULES_SHA"))
    if snapshot is None:
        return Preparation("no_reviewable_changes", trigger, owner)
    if run_limit(environment.get("YDBDOC_REVIEW_MAX_RUN_COST_RUB")) == 0:
        return Preparation("paid_review_disabled", trigger, owner)
    if not store.claim(trigger.pr, trigger.head_sha, trigger.request_id, owner):
        return Preparation("pr_busy", trigger, owner)
    return Preparation("ready", trigger, owner, snapshot)


def run_worker(
    preparation: Preparation, github: ReviewGitHub, store: ReviewStore,
    environment: Mapping[str, str], transport: HttpTransport,
) -> dict[str, object]:
    if preparation.status != "ready" or preparation.snapshot is None:
        raise ReviewError("review_worker_not_admitted")
    trigger, owner = preparation.trigger, preparation.owner
    if preparation.snapshot.head_sha != trigger.head_sha:
        raise ReviewError("review_worker_head_mismatch")

    def guard() -> None:
        pr = github.read_pr(trigger.pr)
        if not pr.open or pr.draft:
            raise ReviewError("cancelled")
        if pr.head_sha != trigger.head_sha:
            raise ReviewError("superseded")
        if not store.owns(trigger.pr, trigger.head_sha, owner):
            raise ReviewError("review_lease_lost")

    last_attempt = 0

    def audit(event: BudgetEvent) -> None:
        nonlocal last_attempt
        if event.kind == "reserved":
            last_attempt = event.attempt_id
        store.budget(owner, event)

    def record(attempt: AttemptResult) -> None:
        store.attempt(owner, last_attempt, attempt)

    budget = RunBudget.from_environment(environment, audit)
    try:
        guard()
        store.configure(owner, {"status": "running", "head_sha": trigger.head_sha,
                                "limit_rub": str(budget.limit),
                                "action_sha": environment.get("YDBDOC_REVIEW_ACTION_SHA", "unreleased"),
                                "runtime_revision": environment.get("YDBDOC_REVIEW_REVISION", "unreleased"),
                                "image_digest": environment.get("YDBDOC_REVIEW_IMAGE_DIGEST", "unreleased")})
        model = BudgetedPolicyModel(
            YandexCredentials(environment.get("YANDEX_API_KEY", ""),
                              environment.get("YANDEX_FOLDER_ID", "")),
            transport, budget, record, before_attempt=guard,
        )
        report: dict[str, object] = review_snapshot(preparation.snapshot, model=model).to_json()
        guard()
    except Exception as error:  # noqa: BLE001 - secret-bearing config/provider failures.
        report = {"status": error.code if isinstance(error, ReviewError) else "infrastructure_error",
                  "head_sha": trigger.head_sha,
                  "cost_rub": None if budget.unknown else str(budget.spent),
                  "reserved_rub": str(budget.reserved), "findings": [], "coverage": []}
    report.update({"limit_rub": str(budget.limit), "owner": owner,
                   "known_cost_rub": str(budget.spent),
                   "model": PRODUCTION_MODEL, "pricing_version": PRICING_VERSION,
                   "action_sha": environment.get("YDBDOC_REVIEW_ACTION_SHA", "unreleased"),
                   "runtime_revision": environment.get("YDBDOC_REVIEW_REVISION", "unreleased"),
                   "image_digest": environment.get("YDBDOC_REVIEW_IMAGE_DIGEST", "unreleased")})
    # Finish never deletes a lease with reserved/unknown attempts.
    store.finish(trigger.pr, owner, report)
    return report


def queue_review(trigger: ReviewTrigger, owner: str, github: ReviewGitHub,
                 store: ReviewStore, environment: Mapping[str, str]) -> str:
    """Run before entering a cancelling Actions concurrency group."""
    status = admit(trigger, github, store)
    if status != "admitted":
        return status
    pr = github.read_pr(trigger.pr)
    if not any(str(r.get("filename", "")).startswith("ydb/docs/")
               or str(r.get("previous_filename", "")).startswith("ydb/docs/") for r in github.files(pr)):
        return "no_reviewable_changes"
    if run_limit(environment.get("YDBDOC_REVIEW_MAX_RUN_COST_RUB")) == 0:
        return "paid_review_disabled"
    return "admitted" if store.queue(trigger.pr, trigger.head_sha, trigger.request_id, owner) else "duplicate"


def summary(report: Mapping[str, Any]) -> str:
    status = html.escape(str(report.get("status", "infrastructure_error")))
    cost = report.get("cost_rub")
    parts = [f"Состояние: **{status}**.",
             "Расход: " + (html.escape(str(cost)) + " RUB" if cost is not None else "неизвестен")
             + "; лимит: " + html.escape(str(report.get("limit_rub", "не задан"))) + " RUB."]
    parts.append("Повторная проверка: поставьте метку `doc_review`.")
    incomplete = [c for c in report.get("coverage", []) if c.get("status") == "not_checked"]
    if incomplete:
        parts.append(f"Проверка неполная: непроверенных пунктов — {len(incomplete)}.")
    if cost is None:
        parts.append("Подтверждённый расход: " + html.escape(str(report.get("known_cost_rub", "неизвестен")))
                     + " RUB; незавершённый резерв: " + html.escape(str(report.get("reserved_rub", "неизвестен")))
                     + " RUB.")
    for finding in report.get("findings", []):
        parts.append("\n" + html.escape(str(finding["rule_id"])) + " — "
                     + html.escape(str(finding["path"])) + ":" + str(finding["line"]) + "\n\n"
                     + html.escape(str(finding["explanation"])) + "\n\n"
                     + "> " + html.escape(str(finding["quote"])) + "\n\n"
                     + html.escape(str(finding["suggestion"])))
    body = "\n\n".join(parts)
    if len(body) > 55000:
        body = body[:55000] + "\n\nОтчёт сокращён; полный результат сохранён в аудите."
    return body
