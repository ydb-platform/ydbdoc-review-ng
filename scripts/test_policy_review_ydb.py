"""Integration smoke against an anonymous disposable /local database only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import ydb

from ydbdoc_review_ng.policy_review.budget import BudgetEvent
from ydbdoc_review_ng.policy_review.store import ReviewSDKExecutor, YdbReviewStore


def run() -> None:
    # No environment credentials and no cloud endpoint are used in this smoke.
    with ydb.Driver(endpoint="grpc://127.0.0.1:2136", database="/local",
                    credentials=ydb.AnonymousCredentials()) as driver:
        driver.wait(timeout=120, fail_fast=True)
        with ydb.QuerySessionPool(driver) as pool:
            executor = ReviewSDKExecutor("", "", "", "")
            executor._pool = pool  # Inject the disposable test transport, not production auth.
            store = YdbReviewStore(executor)
            schema = Path(__file__).resolve().parents[1] / "docs/doc-review-schema.sql"
            for statement in schema.read_text().split(";"):
                if statement.strip():
                    executor.execute(statement + ";", {})
            head = "a" * 40
            store.approve(42, head, "maintainer", "test-event")
            assert store.approved(42, head) and not store.approved(42, "b" * 40)

            def contend(number: int) -> bool:
                request, owner = f"manual:{number}", f"owner:{number}"
                assert store.queue(43, head, request, owner)
                return store.claim(43, head, request, owner)

            with ThreadPoolExecutor(max_workers=4) as workers:
                assert sum(workers.map(contend, range(16))) == 1
            assert store.queue(44, head, "auto:head", "paid-owner")
            assert store.queue(44, head, "auto:head", "paid-owner")
            assert not store.queue(44, head, "auto:head", "duplicate-owner")
            assert store.claim(44, head, "auto:head", "paid-owner")
            store.configure("paid-owner", {"status": "running", "limit_rub": "10"})
            store.budget("paid-owner", BudgetEvent("reserved", 1, Decimal(2)))
            store.budget("paid-owner", BudgetEvent("settled", 1, Decimal(2), Decimal("1.25")))
            store.budget("paid-owner", BudgetEvent("reserved", 2, Decimal(3)))
            recovered = store.recover("paid-owner")
            assert Decimal(recovered["known_cost_rub"]) == Decimal("1.25")
            assert Decimal(recovered["reserved_rub"]) == Decimal(3)
            assert recovered["cost_rub"] is None
            store.finish(44, "paid-owner", recovered)
            assert store.owns(44, head, "paid-owner")  # Orphan with a reserve cannot be replaced.
            assert store.queue(44, head, "manual:new", "new-owner")
            assert not store.claim(44, head, "manual:new", "new-owner")
            store.budget("paid-owner", BudgetEvent("settled", 2, Decimal(3), Decimal("0.5")))
            store.finish(44, "paid-owner", {"status": "cancelled", "cost_rub": "1.75"})
            assert not store.owns(44, head, "paid-owner")
            assert store.claim(44, head, "manual:new", "new-owner")
            print("YDB review smoke: approvals, deduplication, atomic PR ownership and unknown reserves passed")


if __name__ == "__main__":
    try:
        run()
    except Exception as error:  # noqa: BLE001 - only anonymous local test diagnostics.
        # This script only talks to anonymous localhost and uses synthetic records.
        # Preserve SQL/SDK diagnostics here to make integration failures actionable.
        context = error
        while context.__context__ is not None:
            context = context.__context__
        raise RuntimeError("Disposable YDB smoke failed: " + str(context)) from None
