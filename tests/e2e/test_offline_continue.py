"""Public CLI through create_runtime, replacing only HTTP and YDB boundaries."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

# Reuse the substantive pinned-snapshot service fixtures used by integration tests.
sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
from _runtime_services import raw_repair_context
from test_continue_review import ReviewServices
from test_continue_translation import CONTEXT, EN, RU, ContinueServices

from ydbdoc_review_ng.cli import main

pytestmark = pytest.mark.e2e


def invoke(services, pr=42):
    return main(["continue", "--pr", str(pr)], factory=services.runtime)


def test_direction_continuation_retries_only_direction_and_preserves_expiry():
    services = ContinueServices(names=("a",), stop="direction")
    old = services.stop_and_continue()
    # stop_and_continue clears stop; keep forcing undetermined for the first retry.
    services.stop = "direction"
    services.rows[old.continuation_id]["created_at"] -= timedelta(days=5)
    old = services.checkpoint()
    assert invoke(services) == 1
    following = services.checkpoint()
    assert services.roles == ["direction"]
    assert following.expires_at == old.expires_at
    assert CONTEXT in services.prompts[0][1]
    services.stop = None
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
    # State v3 stores no accepted file bytes; continue re-translates every pending path.
    assert services.roles == ["translate", "translate", "critic", "critic", "arbiter", "arbiter"]
    assert services.files[EN + "a.md"].endswith(protected)
    assert b"DROP TABLE" not in services.files[EN + "a.md"]
    assert services.files[EN + "b.md"].startswith(b"# Resumed")
    assert services.rows[old.continuation_id]["status"] == "closed"
    assert CONTEXT in services.prompts[0][1]
    assert all(
        CONTEXT in prompt for role, prompt in services.prompts if role in {"critic", "arbiter"}
    )
    assert not any("SUM" in query for query, _ in services.operations)
    job = list(services.jobs.values())[-1]
    assert job["mode"] == "doc_continue" and job["status"] == "succeeded"


def test_review_cli_reviews_all_files_without_retranslation_then_updates_one_verdict(capsys):
    services = ReviewServices(names=("a", "b"))
    old = services.start_review()
    green = services.files[EN + "a.md"]
    services.rows[old.continuation_id]["created_at"] -= timedelta(days=5)
    old = services.checkpoint()
    services.outcomes = {EN + "b.md": ["repair"]}
    assert invoke(services, 43) == 0
    assert services.roles == ["critic", "critic", "arbiter", "arbiter"]
    assert [paths for _, paths, _ in services.calls] == [
        (EN + "a.md",),
        (EN + "b.md",),
        (EN + "a.md",),
        (EN + "b.md",),
    ]
    for role, prompt in services.prompts:
        sources = json.loads(raw_repair_context(prompt, "source-pr-files"))
        targets = json.loads(raw_repair_context(prompt, "translation-pr-files"))
        glossary = json.loads(raw_repair_context(prompt, "project-glossary"))
        assert set(sources) <= {
            RU + "a.md",
            RU + "b.md",
        }
        assert set(targets) <= {
            EN + "a.md",
            EN + "b.md",
        }
        assert len(sources) == len(targets) == 1
        path = next(iter(targets))
        if path == EN + "a.md":
            assert targets[path] == "# Corrected\n\n```sql\nSELECT 1;\n```\n"
            assert sources[RU + "a.md"] == "# Source a\n\n```sql\nSELECT 1;\n```\n"
        else:
            assert sources[RU + "b.md"] == "# Source b\n\nSource detail b\n"
            assert targets[path] == (
                "# Translated\n\nTranslated\n"
                if role == "critic"
                else "# Repaired b\n\nTranslated\n"
            )
        # Dual-locale glossaries without matching section anchors → empty relevant set.
        assert glossary == {}
        assert CONTEXT in prompt
        assert "Prior arbiter sentinel" not in prompt
        assert all(
            CONTEXT not in text for files in (sources, targets, glossary) for text in files.values()
        )
    assert services.files[EN + "a.md"] == green
    assert services.files[EN + "b.md"] == b"# Repaired b\n\nTranslated\n"
    # §4.1: critic chunk already committed/pushed; post-arbiter publish is a no-op
    # when the final candidate matches that head (see integration continue review).
    assert services.timeline == [
        "critic",
        "critic",
        "commit",
        "push",
        "arbiter",
        "arbiter",
        "report",
        "report",
    ]
    assert services.rows[old.continuation_id]["status"] == "closed"
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
