"""The production launcher, gate and publisher with offline GitHub, store and provider."""
import base64
import difflib
import gzip
import hashlib
import importlib.util
import json
from functools import partial
from pathlib import Path

import pytest

from ydbdoc_review_ng.policy_review import commands
from ydbdoc_review_ng.policy_review.agent import SKILL
from ydbdoc_review_ng.policy_review.runtime import prepare_review

from .runtime_fakes import MemoryStore
from .test_debounce import FakeTime
from .test_skill_agent import tools_reply

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/pr_review"


def blob(text):
    raw = text.encode()
    return hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()


def fixture_changes(fixture):
    rows, bodies = [], {}
    for item in fixture["files"]:
        path, after = item["path"], item["after"]
        assert blob(after) == item["head_blob"]
        before = after.splitlines(keepends=True)
        for r in reversed(item["replacements"]):
            start = r["head_line"] - 1
            before[start:start + r["line_count"]] = r["before"].splitlines(keepends=True)
        assert blob("".join(before)) == item["base_blob"]
        patch = list(difflib.unified_diff(before, after.splitlines(keepends=True)))[2:]
        rows.append({"filename": path, "status": "modified", "patch": "".join(patch)})
        bodies[path] = after
    return rows, bodies


def test_action_runs_full_public_pr_with_ordered_policy_on_exact_wires(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("trusted_review", ROOT / "scripts/run_policy_review.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher.signal, "signal", lambda *_: None)
    with gzip.open(FIXTURES / "pr60931/public-changes.json.gz") as stream:
        fixture = json.load(stream)
    rows, bodies = fixture_changes(fixture)
    assert len(rows) == 18
    manifest = json.loads((FIXTURES / "writing-policy/source.json").read_text())
    policy = {}
    for record in manifest["files"]:
        p = record["path"]
        value = (FIXTURES / "writing-policy" / Path(p).name).read_bytes()
        assert hashlib.sha256(value).hexdigest() == record["sha256"]
        policy[p] = value.decode()

    head = fixture["head_sha"]
    pr = {"number": 60931, "user": {"login": "fixture-author"}, "head": {"sha": head},
          "base": {"sha": fixture["base_tip_sha"], "repo": {"full_name": "ydb-platform/ydb"}},
          "state": "open", "draft": False, "changed_files": len(rows), "title": fixture["title"],
          "body": fixture["description"]}
    event = {"action": "opened", "repository": {"full_name": "ydb-platform/ydb"},
             "pull_request": pr, "sender": {"login": "fixture-author"}}
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event))
    for k, v in {
        "GITHUB_RUN_ID": "314", "GITHUB_RUN_ATTEMPT": "1", "RUNNER_TEMP": str(tmp_path),
        "GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event_file),
        "YANDEX_API_KEY": "test-secret", "YANDEX_FOLDER_ID": "test-folder",
        "YDBDOC_REVIEW_RULES_SHA": manifest["rules_sha"], "YDBDOC_REVIEW_MAX_RUN_COST_RUB": "100",
        "YDBDOC_REVIEW_READ_TOKEN": "test-secret", "GITHUB_OUTPUT": str(tmp_path / "output"),
    }.items():
        monkeypatch.setenv(k, v)
    writes, reads, wires, reserves = [], [], [], []
    store = MemoryStore()
    timer = FakeTime()
    path = rows[0]["filename"]
    historical = rows[6]["filename"]
    unrelated_history = next(line for line in bodies[historical].splitlines()[100:]
                             if len(line) > 100 and line not in str(rows))
    def api(method, route, payload=None):
        if method != "GET":
            writes.append((route, payload))
            return {}
        if route.endswith("/pulls/60931"):
            return pr
        if "/collaborators/" in route:
            return True
        if "/files?" in route:
            return rows
        if "/comments?" in route or "/reviews?" in route:
            return []
        if route == "/user":
            return {"login": "publisher"}
        if "/contents/" in route:
            filename, revision = route.split("/contents/")[1].split("?ref=")
            assert filename in policy or filename == path
            assert revision == (manifest["rules_sha"] if filename in policy else head)
            raw = (policy[filename] if filename in policy else bodies[filename]).encode()
            reads.append(filename)
            return {"type": "file", "encoding": "base64", "size": len(raw),
                    "content": base64.b64encode(raw).decode()}
        pytest.fail("Unexpected GitHub route")
    def transport(request):
        wire = json.loads(request.body)
        wires.append(len(request.body))
        reservation = store.events["github-run:314:1"][len(wires)]
        assert reservation.kind == "reserved"
        reserves.append(str(reservation.reservation_rub))
        assert "test-secret" not in request.body.decode()
        assert unrelated_history not in request.body.decode() and wire["max_tokens"] == 8192
        if len(wires) == 1:
            assert wire["messages"][0]["content"].startswith((SKILL / "SKILL.md").read_text())
            refs = json.loads(wire["messages"][0]["content"].split("\n\nTrusted skill references:\n", 1)[1])
            assert list(refs["writing-policy"]["files"].items()) == list(policy.items())
            assert refs["writing-policy"]["revision"] == manifest["rules_sha"]
            assert refs["public-map"]["text"] == (SKILL / "public-map.md").read_text()
            assert len(json.loads(wire["messages"][1]["content"])["changes"]) == 18
            return tools_reply(("read_file", {"path": path, "revision": "head",
                                              "start_line": 1, "end_line": 4}))
        replies = [json.loads(m["content"]) for m in wire["messages"] if m["role"] == "tool"]
        assert replies[-1]["revision"] == head and replies[-1]["lines"][0][0] == 1
        return tools_reply(("finish_review", {
            "summary": "Контрольный прогон PR 60931.", "findings": [],
            "checked": "18 diff, политика и выбранный фрагмент.", "gaps": ["Предпросмотр."]}))
    monkeypatch.setattr(launcher, "action_revision", lambda *_: "f" * 40)
    monkeypatch.setattr(commands, "ReviewHTTP", lambda *_, **kwargs: api)
    monkeypatch.setattr(commands, "_sdk", lambda *_: None)
    monkeypatch.setattr(commands, "YdbReviewStore", lambda *_: store)
    monkeypatch.setattr(commands, "UrllibTransport", lambda: transport)
    monkeypatch.setattr(commands, "prepare_review", partial(prepare_review, clock=timer.clock, wait=timer.wait))
    for mode in ["gate", "run"]:
        monkeypatch.setenv("YDBDOC_POLICY_MODE", mode)
        assert launcher.run() == 0
    assert reads == [*policy, path] and len(wires) == 2
    report = store.reports["github-run:314:1"]
    assert report["execution"] == "trusted-action-host" and report["runtime_revision"] == "f" * 40
    assert report["status"] == "incomplete" and report["head_sha"] == head
    assert len(writes) == 2 and writes[1][0].endswith("/issues/60931/comments")
    assert "Контрольный прогон PR 60931." in writes[1][1]["body"]
    assert "Оценка стоимости по тарифу: **0.08 RUB**" in writes[1][1]["body"]
    print("Offline PR 60931 wire bytes", wires, "reservations RUB", reserves)
