"""Trusted action launcher; PR code is never checked out or executed."""

from __future__ import annotations

import os
import re
import signal
import sys
from pathlib import Path

from ydbdoc_review_ng.policy_review.commands import main, read_json
from ydbdoc_review_ng.policy_review.github import ReviewHTTP
from ydbdoc_review_ng.policy_review.types import ReviewError
from ydbdoc_review_ng.policy_review.version import action_revision


def run() -> int:
    root = Path(__file__).resolve().parents[1]
    os.environ["YDBDOC_REVIEW_ACTION_SHA"] = action_revision(root, ReviewHTTP(
        os.environ.get("YDBDOC_REVIEW_READ_TOKEN", ""), repository="ydb-platform/ydbdoc-review-ng"))
    mode = os.environ.get("YDBDOC_POLICY_MODE", "run")
    if mode == "finalize":
        event = read_json(Path(os.environ["GITHUB_EVENT_PATH"]))
        if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_run"
                or event["repository"]["full_name"] != "ydb-platform/ydb"):
            raise ReviewError("unsupported_review_event")
        return main(["finalize", "--run-id", str(event["workflow_run"]["id"])])
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if re.fullmatch(r"[1-9][0-9]*", run_id) is None or re.fullmatch(r"[1-9][0-9]*", attempt) is None:
        raise ReviewError("invalid_review_owner")
    workspace = Path(os.environ["RUNNER_TEMP"]) / ("policy-review-" + run_id + "-" + attempt)
    input_dir, output_dir = workspace / "input", workspace / "output"
    input_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if mode == "gate":
        result = main(["gate", "--event", os.environ["GITHUB_EVENT_PATH"], "--directory", str(input_dir)])
        if result == 0 and read_json(input_dir / "plan.json")["status"] not in {
            "admitted", "ignored", "duplicate", "superseded"
        }:
            return main(["publish", "--directory", str(input_dir), "--report", str(output_dir / "report.json")])
        return result
    if mode != "run":
        raise ReviewError("unsupported_review_mode")
    result = 1

    def interrupted(signum: int, frame: object) -> None:
        raise InterruptedError

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        result = main(["prepare", "--event", os.environ["GITHUB_EVENT_PATH"], "--directory", str(input_dir)])
        if result == 0 and read_json(input_dir / "plan.json")["status"] == "ready":
            result = main(["worker", "--input", str(input_dir), "--output", str(output_dir)])
    finally:
        if (input_dir / "plan.json").exists():
            published = main(["publish", "--directory", str(input_dir),
                              "--report", str(output_dir / "report.json")])
            result = result or published
    return result


if __name__ == "__main__":
    try:
        sys.exit(run())
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        print("Documentation review launcher failed: " + (
            error.code if isinstance(error, ReviewError) else "infrastructure_error"), file=sys.stderr)
        sys.exit(1)
