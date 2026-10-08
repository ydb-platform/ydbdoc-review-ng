"""Trusted action launcher; PR code is never checked out or executed."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from ydbdoc_review_ng.policy_review.commands import main, read_json
from ydbdoc_review_ng.policy_review.github import ReviewHTTP
from ydbdoc_review_ng.policy_review.types import ReviewError
from ydbdoc_review_ng.policy_review.version import action_revision

_WORKER_ENV = (
    "YANDEX_API_KEY", "YANDEX_FOLDER_ID", "YDB_ENDPOINT", "YDB_DATABASE", "YDB_TOKEN", "YDB_SA_KEY",
    "YDBDOC_REVIEW_MAX_RUN_COST_RUB",
    "YDBDOC_REVIEW_REVISION", "YDBDOC_REVIEW_IMAGE_DIGEST",
    "YDBDOC_REVIEW_ACTION_SHA",
)


def docker_arguments(image: str, name: str, input_dir: Path, output_dir: Path,
                     uid: int, gid: int) -> list[str]:
    if type(image) is not str or re.fullmatch(
        r"ghcr\.io/ydb-platform/ydbdoc-review-ng@sha256:[0-9a-f]{64}", image
    ) is None:
        raise ReviewError("review_image_not_released")
    if uid <= 0 or gid <= 0:
        raise ReviewError("review_runner_must_not_be_root")
    return ["docker", "run", "--name", name, "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=1g", "--cpus=1",
            "--user", f"{uid}:{gid}", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "--mount", f"type=bind,src={input_dir},dst=/review/input,readonly",
            "--mount", f"type=bind,src={output_dir},dst=/review/output",
            *(arg for key in _WORKER_ENV for arg in ("--env", key)),
            image, "worker", "--input", "/review/input", "--output", "/review/output"]


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
    image = json.loads((Path(__file__).resolve().parents[1] / "docker/review-image.json").read_text())
    name = "policy-review-" + run_id + "-" + attempt
    docker_started = False
    stopped = True
    result = 1

    def interrupted(signum: int, frame: object) -> None:
        raise InterruptedError

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        result = main(["prepare", "--event", os.environ["GITHUB_EVENT_PATH"], "--directory", str(input_dir)])
        if result == 0 and read_json(input_dir / "plan.json")["status"] == "ready":
            args = docker_arguments(image["image"], name, input_dir, output_dir, os.getuid(), os.getgid())
            revision = image["revision"]
            if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                raise ReviewError("review_image_not_released")
            os.environ["YDBDOC_REVIEW_REVISION"] = revision
            os.environ["YDBDOC_REVIEW_IMAGE_DIGEST"] = image["image"].split("@", 1)[1]
            subprocess.run(["docker", "pull", image["image"]], check=True, timeout=300)
            inspected = subprocess.run(["docker", "inspect", image["image"]], check=True,
                                       capture_output=True, text=True, timeout=30)
            actual_revision = json.loads(inspected.stdout)[0]["Config"]["Labels"][
                "org.opencontainers.image.revision"]
            if actual_revision != revision:
                raise ReviewError("review_image_revision_mismatch")
            docker_started, stopped = True, False
            result = subprocess.run(args, check=False, timeout=1800).returncode
    finally:
        if docker_started:
            stopped = subprocess.run(["docker", "rm", "--force", name],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     check=False, timeout=30).returncode == 0
        if stopped and (input_dir / "plan.json").exists():
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
