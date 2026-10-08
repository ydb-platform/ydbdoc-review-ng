from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.policy_review.helpers import snapshot_json
from ydbdoc_review_ng.cli import main

pytestmark = pytest.mark.unit


def test_review_cli_works_offline_and_writes_grounded_report(tmp_path: Path) -> None:
    input_path, output = tmp_path / "snapshot.json", tmp_path / "report.json"
    input_path.write_text(json.dumps(snapshot_json(after="# Test\n\n```sql\nselect 1;\n```\n")))
    assert main(["review", "--snapshot", str(input_path), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["status"] == "formal_only" and report["cost_rub"] == "0"
    assert report["findings"][0]["rule_id"] == "FORMAT.YQL"


def test_bad_snapshot_diagnostic_does_not_echo_untrusted_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"secret-input": "secret-output"}))
    assert main(["review", "--snapshot", str(path), "--output", str(tmp_path / "out.json")]) == 1
    captured = capsys.readouterr()
    assert "secret-" not in captured.err and "invalid_snapshot_schema" in captured.err
