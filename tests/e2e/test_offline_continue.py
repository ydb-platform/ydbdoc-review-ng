"""Public CLI through create_runtime, replacing only HTTP and YDB boundaries."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

# Reuse the substantive pinned-snapshot service fixtures used by integration tests.
sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
from test_continue_review import ReviewServices
from test_continue_translation import CONTEXT, EN, RU, ContinueServices

from ydbdoc_review_ng.cli import main

pytestmark = pytest.mark.e2e


def invoke(services, pr=42):
    return main(["continue", "--pr", str(pr)], factory=services.runtime)


def test_direction_continuation_retries_only_direction_and_preserves_expiry():
    services = ContinueServices(names=("a",), stop="direction")
    old = services.stop_and_continue()
    services.rows[old.continuation_id]["created_at"] -= timedelta(days=5)
    old = services.checkpoint()
    assert invoke(services) == 1
    following = services.checkpoint()
    assert services.roles == ["direction"]
    assert following.expires_at == old.expires_at
    assert CONTEXT in services.prompts[0][1]
    services.direction_values = {"a.md": "complete_pair"}
    assert invoke(services) == 0
    assert services.roles == ["direction", "direction"]
    assert services.rows[following.continuation_id]["status"] == "closed"
    assert services.commits == 0


def test_pending_cli_continuation_reuses_green_map_and_source_only_protected_bytes():
    services = ContinueServices(names=("a", "b"), stop="translation")
    protected = b"\n```sql\nSELECT 1;\n```\n"
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + "a.md"] += protected
        tree[EN + "a.md"] = b"# Poison\n```sql\nDROP TABLE t;\n```\n"
    old = services.stop_and_continue()
    assert invoke(services) == 0
    assert services.roles == ["translate", "critic", "critic"]
    assert services.files[EN + "a.md"] == b"# Translated\n" + protected
    assert services.files[EN + "b.md"] == b"# Resumed b\n"
    assert services.rows[old.continuation_id]["status"] == "closed"
    assert CONTEXT in services.prompts[0][1]
    assert all(CONTEXT not in prompt for role, prompt in services.prompts if role == "critic")
    assert not any("SUM" in query for query, _ in services.operations)
    job = list(services.jobs.values())[-1]
    assert job["mode"] == "doc_continue" and job["status"] == "succeeded"


def test_review_cli_repairs_only_unresolved_path_then_updates_one_verdict(capsys):
    services = ReviewServices(names=("a", "b"))
    old = services.start_review()
    green = services.files[EN + "a.md"]
    services.rows[old.continuation_id]["created_at"] -= timedelta(days=5)
    old = services.checkpoint()
    services.outcomes = {EN + "b.md": ["repair", "red"]}
    assert invoke(services, 43) == 1
    following = services.checkpoint()
    assert services.roles == ["critic", "critic"]
    assert {path for _, path, _ in services.calls} == {EN + "b.md"}
    assert services.files[EN + "a.md"] == green
    assert services.files[EN + "b.md"] == b"# Repaired b\n\nTranslated\n"
    assert following.expires_at == old.expires_at
    assert services.timeline == [
        "critic",
        "commit",
        "push",
        "critic",
        "report",
        "checkpoint",
    ]
    assert invoke(services, 43) == 0
    assert services.rows[following.continuation_id]["status"] == "closed"
    assert len(services.comments) == 1
    assert services.comments[0]["body"].startswith("🟢 GREEN\n")
    assert CONTEXT not in services.comments[0]["body"]
    captured = capsys.readouterr()
    assert CONTEXT not in captured.out + captured.err


@pytest.mark.parametrize(
    "fault", ["missing", "empty", "unauthorized", "label-actor", "late", "expired", "stale"]
)
def test_continue_rejects_before_models_or_mutations_but_terminalizes_audit(fault):
    services = ReviewServices(names=("a", "b"))
    saved = services.start_review()
    if fault == "missing":
        services.commands.clear()
    elif fault == "empty":
        services.commands[0]["body"] = "/ydbdoc continue\n   "
    elif fault == "unauthorized":
        services.commands[0]["user"]["login"] = "outsider"
    elif fault == "label-actor":
        github = services.github

        def denied_label(method, path, payload):
            response = github(method, path, payload)
            if path.endswith("/events?per_page=100"):
                response[0]["actor"]["login"] = "outsider"
            return response

        services.github = denied_label
    elif fault == "late":
        services.commands[0]["created_at"] = "2026-09-21T12:00:00Z"
        services.commands[0]["updated_at"] = "2026-09-21T12:00:00Z"
    elif fault == "expired":
        services.rows[saved.continuation_id]["created_at"] -= timedelta(days=15)
    else:
        services.branch_head = "f" * 40
    assert invoke(services, 43) == 1
    assert services.roles == []
    assert not any(method in {"POST", "PATCH"} for method, _ in services.events)
    assert list(services.jobs.values())[-1]["status"] == "failed"
    assert services.rows[saved.continuation_id]["status"] == "open"
