"""Persistent approvals, idempotent claims, exclusive PR ownership and money audit."""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol

from ydbdoc_review_ng.models import AttemptResult
from ydbdoc_review_ng.models.configuration import PRICING_VERSION
from ydbdoc_review_ng.persistence import YdbExecutor
from ydbdoc_review_ng.policy_review.admission import REPOSITORY
from ydbdoc_review_ng.policy_review.budget import BudgetEvent, money
from ydbdoc_review_ng.policy_review.types import ReviewError


class ReviewStore(Protocol):
    def approve(self, pr: int, head: str, actor: str, event_id: str) -> None: ...
    def approved(self, pr: int, head: str) -> bool: ...
    def queue(self, pr: int, head: str, request_id: str, owner: str) -> bool: ...
    def claim(self, pr: int, head: str, request_id: str, owner: str) -> bool: ...
    def owns(self, pr: int, head: str, owner: str) -> bool: ...
    def budget(self, owner: str, event: BudgetEvent) -> None: ...
    def attempt(self, owner: str, number: int, attempt: AttemptResult) -> None: ...
    def configure(self, owner: str, metadata: Mapping[str, object]) -> None: ...
    def finish(self, pr: int, owner: str, report: Mapping[str, object]) -> None: ...


# Each method is one serializable read/write query. No lease timeout: an orphan
# with an unresolved paid reservation stays locked until financial reconciliation.
_QUEUE = """
$previous = SELECT COUNT(*) FROM doc_review_claims
 WHERE repository=$repository AND pr=$pr AND request_id=$request_id;
$mine = SELECT COUNT(*) FROM doc_review_claims
 WHERE repository=$repository AND pr=$pr AND request_id=$request_id AND owner=$owner AND phase='waiting';
UPSERT INTO doc_review_claims (repository, pr, request_id, owner, phase)
SELECT $repository AS repository, $pr AS pr, $request_id AS request_id,
 $owner AS owner, 'waiting' AS phase WHERE $previous == 0;
UPSERT INTO doc_review_runs (owner, repository, pr, head, status, updated_at, report)
SELECT $owner AS owner, $repository AS repository, $pr AS pr, $head AS head,
 'waiting' AS status, $now AS updated_at, $empty_report AS report WHERE $previous == 0;
SELECT $previous == 0 OR $mine == 1 AS accepted;
"""
_CLAIM = """
$leases = SELECT COUNT(*) FROM doc_review_leases WHERE repository=$repository AND pr=$pr;
$claims = SELECT COUNT(*) FROM doc_review_claims
  WHERE repository=$repository AND pr=$pr AND request_id=$request_id AND owner=$owner AND phase='waiting';
$eligible = $leases == 0 AND $claims == 1;
UPSERT INTO doc_review_leases (repository, pr, head, owner)
SELECT $repository AS repository, $pr AS pr, $head AS head, $owner AS owner WHERE $eligible;
UPSERT INTO doc_review_claims (repository, pr, request_id, owner, phase)
SELECT $repository AS repository, $pr AS pr, $request_id AS request_id,
 $owner AS owner, 'running' AS phase WHERE $eligible;
UPSERT INTO doc_review_runs (owner, repository, pr, head, status, updated_at, report)
SELECT $owner AS owner, $repository AS repository, $pr AS pr, $head AS head,
 'prepared' AS status, $now AS updated_at, $empty_report AS report WHERE $eligible;
SELECT $eligible AS accepted;
"""
_RESERVE = """
$held = SELECT COUNT(*) FROM doc_review_leases WHERE owner=$owner;
$previous = SELECT COUNT(*) FROM doc_review_attempts WHERE owner=$owner AND attempt=$attempt;
UPSERT INTO doc_review_attempts (owner, attempt, kind, reservation, cost, updated_at)
SELECT $owner AS owner, $attempt AS attempt, $kind AS kind, $reservation AS reservation,
 $cost AS cost, $now AS updated_at WHERE $held == 1 AND $previous == 0;
SELECT $held == 1 AND $previous == 0 AS accepted;
"""
_SETTLE = """
$existing = SELECT COUNT(*) FROM doc_review_attempts
WHERE owner=$owner AND attempt=$attempt AND kind IN ('reserved', 'unknown');
UPDATE doc_review_attempts SET kind=$kind, cost=$cost, updated_at=$now
WHERE owner=$owner AND attempt=$attempt AND $existing == 1;
SELECT $existing == 1 AS accepted;
"""
_FINISH = """
UPDATE doc_review_runs SET status=$status, updated_at=$now, report=$report WHERE owner=$owner;
$pending = SELECT COUNT(*) FROM doc_review_attempts WHERE owner=$owner AND kind != 'settled';
DELETE FROM doc_review_leases
WHERE repository=$repository AND pr=$pr AND owner=$owner AND $pending == 0;
DELETE FROM doc_review_claims WHERE repository=$repository AND pr=$pr AND owner=$owner AND phase='waiting';
"""


class YdbReviewStore:
    def __init__(self, executor: YdbExecutor) -> None:
        self.executor = executor

    def _query(self, statement: str, **parameters: object) -> Sequence[Mapping[str, object]]:
        try:
            return self.executor.execute(statement, parameters)
        except Exception:  # noqa: BLE001 - never expose SDK payloads or credentials.
            raise ReviewError("review_audit_unavailable") from None

    def approve(self, pr: int, head: str, actor: str, event_id: str) -> None:
        self._query("""UPSERT INTO doc_review_approvals
          (repository, pr, head, actor, event_id, approved_at)
          VALUES ($repository, $pr, $head, $actor, $event_id, $now);""",
                    repository=REPOSITORY, pr=pr, head=head, actor=actor,
                    event_id=event_id, now=datetime.now(UTC))

    def approved(self, pr: int, head: str) -> bool:
        return bool(self._query("""SELECT head FROM doc_review_approvals
          WHERE repository=$repository AND pr=$pr AND head=$head;""",
                                repository=REPOSITORY, pr=pr, head=head))

    def claim(self, pr: int, head: str, request_id: str, owner: str) -> bool:
        rows = self._query(_CLAIM, repository=REPOSITORY, pr=pr, head=head,
                           request_id=request_id, owner=owner, now=datetime.now(UTC),
                           empty_report=b"{}")
        return len(rows) == 1 and rows[0].get("accepted") is True

    def queue(self, pr: int, head: str, request_id: str, owner: str) -> bool:
        rows = self._query(_QUEUE, repository=REPOSITORY, pr=pr, head=head,
                           request_id=request_id, owner=owner, now=datetime.now(UTC),
                           empty_report=b"{}")
        return len(rows) == 1 and rows[0].get("accepted") is True

    def owns(self, pr: int, head: str, owner: str) -> bool:
        return bool(self._query("""SELECT owner FROM doc_review_leases
          WHERE repository=$repository AND pr=$pr AND head=$head AND owner=$owner;""",
                                repository=REPOSITORY, pr=pr, head=head, owner=owner))

    def budget(self, owner: str, event: BudgetEvent) -> None:
        rows = self._query(_RESERVE if event.kind == "reserved" else _SETTLE,
                           owner=owner, attempt=event.attempt_id, kind=event.kind,
                           reservation=event.reservation_rub if event.kind == "reserved" else None,
                           cost=event.cost_rub, now=datetime.now(UTC))
        if len(rows) != 1 or rows[0].get("accepted") is not True:
            raise ReviewError("review_audit_conflict")

    def attempt(self, owner: str, number: int, attempt: AttemptResult) -> None:
        # Store metadata/usage, never prompt, response text, credentials or raw payloads.
        usage = json.dumps({"usage": asdict(attempt.usage), "model": attempt.request_model,
                            "pricing_version": PRICING_VERSION,
                            "http_status": attempt.http_status,
                            "status": attempt.status.value,
                            "error": attempt.error.value if attempt.error else None,
                            "started_at": attempt.started_at.isoformat(),
                            "finished_at": attempt.finished_at.isoformat()}).encode()
        self._query("""UPDATE doc_review_attempts SET usage=$usage
          WHERE owner=$owner AND attempt=$attempt;""", owner=owner, attempt=number, usage=usage)

    def finish(self, pr: int, owner: str, report: Mapping[str, object]) -> None:
        # The public summary is stored; full document snapshots are not persisted.
        self._query(_FINISH, repository=REPOSITORY, pr=pr, owner=owner,
                    status=str(report["status"]), now=datetime.now(UTC),
                    report=json.dumps(dict(report), ensure_ascii=False).encode())

    def configure(self, owner: str, metadata: Mapping[str, object]) -> None:
        self._query("UPDATE doc_review_runs SET report=$report WHERE owner=$owner;",
                    owner=owner, report=json.dumps(dict(metadata)).encode())

    def identity(self, owner: str) -> tuple[int, str]:
        rows = self._query("SELECT pr, head FROM doc_review_runs WHERE owner=$owner;", owner=owner)
        if len(rows) != 1 or type(rows[0]["pr"]) is not int or type(rows[0]["head"]) is not str:
            raise ReviewError("review_run_missing")
        return rows[0]["pr"], rows[0]["head"]

    def recover(self, owner: str) -> dict[str, object]:
        runs = self._query("SELECT report FROM doc_review_runs WHERE owner=$owner;", owner=owner)
        if len(runs) != 1:
            raise ReviewError("review_run_missing")
        raw = runs[0].get("report")
        base: dict[str, object] = {}
        if isinstance(raw, (str, bytes)):
            report = json.loads(raw)
            if type(report) is dict:
                base = report
        ledger = self._query("SELECT kind, cost, reservation FROM doc_review_attempts WHERE owner=$owner;",
                             owner=owner)
        from decimal import Decimal

        unknown = any(r["kind"] != "settled" for r in ledger)
        spent = sum((money(r["cost"]) for r in ledger if r["cost"] is not None), Decimal(0))
        reserved = sum((money(r["reservation"]) for r in ledger if r["kind"] != "settled"), Decimal(0))
        if not unknown and base.get("status") not in {None, "waiting", "prepared", "running"}:
            return base
        return {**base, "status": "cost_unknown" if unknown else "cancelled", "owner": owner,
                "cost_rub": None if unknown else str(spent), "reserved_rub": str(reserved),
                "known_cost_rub": str(spent), "findings": [], "coverage": []}


_TYPES = {
    "repository": "Utf8", "head": "Utf8", "owner": "Utf8", "request_id": "Utf8",
    "actor": "Utf8", "event_id": "Utf8", "status": "Utf8", "kind": "Utf8",
    "pr": "Uint64", "attempt": "Uint64", "now": "Timestamp",
    "reservation": "Decimal", "cost": "Decimal",
    "report": "String", "empty_report": "String", "usage": "String",
}


class ReviewSDKExecutor:
    """Dedicated SDK boundary; translator's parameter typing is left untouched."""

    def __init__(self, endpoint: str, database: str, token: str, service_account_key: str) -> None:
        self._endpoint, self._database = endpoint, database
        self._token, self._key = token, service_account_key
        self._pool: Any = None
        self._driver: Any = None

    def execute(self, statement: str, parameters: Mapping[str, object], /) -> Sequence[Mapping[str, object]]:
        try:
            sdk = importlib.import_module("ydb")
            if self._pool is None:
                if not self._endpoint or not self._database or not (self._token or self._key):
                    raise ValueError
                credentials = sdk.AccessTokenCredentials(self._token) if self._token else (
                    sdk.iam.ServiceAccountCredentials.from_content(self._key)
                )
                self._driver = sdk.Driver(endpoint=self._endpoint, database=self._database,
                                          credentials=credentials)
                self._driver.wait(timeout=30, fail_fast=True)
                self._pool = sdk.QuerySessionPool(self._driver)
            declarations, values = [], {}
            for name, value in parameters.items():
                kind = _TYPES[name]  # fixed whitelist, never a name from PR content
                optional = kind == "Decimal"
                value_type = sdk.OptionalType(sdk.DecimalType(22, 9)) if optional else (
                    getattr(sdk.PrimitiveType, kind)
                )
                declarations.append(f"DECLARE ${name} AS " + ("Decimal(22,9)?" if optional else kind) + ";")
                values["$" + name] = sdk.TypedValue(value, value_type)
            results = self._pool.execute_with_retries("\n".join(declarations) + "\n" + statement, values)
            return [dict(row) for result in results for row in result.rows]
        except Exception:  # noqa: BLE001 - SDK diagnostics may include secrets.
            raise ReviewError("review_audit_unavailable") from None

    def close(self) -> None:
        try:
            if self._pool is not None:
                self._pool.stop()
            if self._driver is not None:
                self._driver.stop()
        except Exception:  # noqa: BLE001
            raise ReviewError("review_audit_unavailable") from None
