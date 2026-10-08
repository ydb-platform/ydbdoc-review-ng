"""Trusted deployment CLI: prepare, worker, publish; offline review CLI is unchanged."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

from ydbdoc_review_ng.models import UrllibTransport
from ydbdoc_review_ng.policy_review.admission import ReviewTrigger
from ydbdoc_review_ng.policy_review.budget import run_limit
from ydbdoc_review_ng.policy_review.github import PREFIX, ReviewGitHub, ReviewHTTP
from ydbdoc_review_ng.policy_review.runtime import (
    Preparation,
    prepare_review,
    queue_review,
    run_worker,
    summary,
)
from ydbdoc_review_ng.policy_review.store import ReviewSDKExecutor, YdbReviewStore
from ydbdoc_review_ng.policy_review.types import ReviewError, ReviewSnapshot
from ydbdoc_review_ng.runtime_ydb import DEFAULT_YDB_DATABASE, DEFAULT_YDB_ENDPOINT


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, "Invalid documentation review command\n")


def read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        body = stream.read(25_000_001)
    if len(body) > 25_000_000:
        raise ReviewError("review_input_limit_exceeded")
    return json.loads(body)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_preparation(directory: Path, *, with_snapshot: bool = False) -> Preparation:
    plan = read_json(directory / "plan.json")
    trigger = ReviewTrigger(**plan["trigger"])
    owner = plan["owner"]
    if type(owner) is not str or re.fullmatch(r"github-run:[0-9]+:[0-9]+", owner) is None:
        raise ReviewError("invalid_review_owner")
    snapshot = ReviewSnapshot.from_json(read_json(directory / "snapshot.json")) if with_snapshot else None
    return Preparation(plan["status"], trigger, owner, snapshot)


def _sdk(env: Mapping[str, str]) -> ReviewSDKExecutor:
    return ReviewSDKExecutor(
        env.get("YDB_ENDPOINT") or DEFAULT_YDB_ENDPOINT,
        env.get("YDB_DATABASE") or DEFAULT_YDB_DATABASE,
        env.get("YDB_TOKEN", ""), env.get("YDB_SA_KEY", ""),
    )


def main(argv: list[str] | None = None) -> int:
    parser = _Parser()
    sub = parser.add_subparsers(dest="mode", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--event", required=True)
    prepare.add_argument("--directory", required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--event", required=True)
    gate.add_argument("--directory", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--input", required=True)
    worker.add_argument("--output", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--directory", required=True)
    publish.add_argument("--report", required=True)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--run-id", required=True)
    schema = sub.add_parser("init-schema")
    schema.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    sdk = _sdk(env)
    store = YdbReviewStore(sdk)
    github = ReviewGitHub(ReviewHTTP(env.get("YDBDOC_REVIEW_READ_TOKEN", ""),
                                   anonymous_pr_read=args.mode == "worker"),
                          ReviewHTTP(env.get("YDBDOC_REVIEW_ADMISSION_TOKEN", "")),
                          ReviewHTTP(env.get("YDBDOC_REVIEW_PUBLISH_TOKEN", "")),
                          ReviewHTTP(env.get("YDBDOC_REVIEW_CHECKS_TOKEN", "")))
    preparation: Preparation | None = None
    try:
        if args.mode == "init-schema":
            body = Path(args.file).read_text()
            for statement in body.split(";"):
                if statement.strip():
                    sdk.execute(statement + ";", {})
            return 0
        if args.mode == "finalize":
            if re.fullmatch(r"[1-9][0-9]{0,19}", args.run_id) is None:
                raise ReviewError("invalid_review_owner")
            completed = ReviewHTTP(env.get("YDBDOC_REVIEW_READ_TOKEN", ""))(
                "GET", PREFIX + "/actions/runs/" + args.run_id)
            if (not isinstance(completed, dict) or completed.get("status") != "completed"
                    or completed.get("event") != "pull_request_target"
                    or completed.get("name") != "YDB documentation policy review"):
                raise ReviewError("review_run_not_completed")
            attempts = completed["run_attempt"]
            if type(attempts) is not int or not 1 <= attempts <= 100:
                raise ReviewError("invalid_review_owner")
            for number in range(1, attempts + 1):
                owner = "github-run:" + args.run_id + ":" + str(number)
                try:
                    pr, head = store.identity(owner)
                except ReviewError as error:
                    if error.code == "review_run_missing":
                        continue
                    raise
                report = store.recover(owner)
                report["head_sha"] = head
                store.finish(pr, owner, report)
                if github.read_pr(pr).head_sha == head:
                    github.publish(pr, head, summary(report),
                                   "success" if report["status"] == "clean" else "neutral")
            return 0
        if args.mode in {"prepare", "gate"}:
            directory = Path(args.directory)
            directory.mkdir(parents=True, exist_ok=True)
            event = ReviewTrigger.parse(read_json(Path(args.event)), env.get("GITHUB_EVENT_NAME", ""),
                                        env.get("GITHUB_RUN_ID", ""))
            if event is None:
                write_json(directory / "plan.json", {"status": "ignored"})
                return 0
            attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
            if re.fullmatch(r"[1-9][0-9]{0,9}", attempt) is None:
                raise ReviewError("invalid_review_owner")
            owner = event.event_id + ":" + attempt
            # Persist safe metadata before network/DB calls for finalization on failures.
            preparation = Preparation("infrastructure_error", event, owner)
            write_json(directory / "plan.json", preparation.plan())
            if args.mode == "gate":
                status = queue_review(event, owner, github, store, env)
                preparation = Preparation(status, event, owner)
            else:
                preparation = prepare_review(event, owner, github, store, env)
            write_json(directory / "plan.json", preparation.plan())
            if preparation.snapshot is not None:
                snapshot = asdict(preparation.snapshot)
                snapshot["rules"] = dict(preparation.snapshot.rules)
                snapshot["context"] = dict(preparation.snapshot.context)
                write_json(directory / "snapshot.json", snapshot)
            if env.get("GITHUB_OUTPUT"):
                with Path(env["GITHUB_OUTPUT"]).open("a") as output:
                    output.write("ready=" + ("true" if preparation.status == "ready" else "false") + "\n")
                    output.write("admitted=" + ("true" if preparation.status == "admitted" else "false") + "\n")
                    output.write("stop=" + ("true" if event.action in {"closed", "converted_to_draft"}
                                             or event.action == "synchronize" and preparation.status in {
                                                 "waiting_for_approval", "paid_review_disabled", "no_reviewable_changes"}
                                             else "false") + "\n")
            if args.mode == "prepare" and preparation.status not in {"ready", "duplicate"}:
                store.finish(event.pr, owner, {"status": preparation.status, "cost_rub": "0",
                                              "reserved_rub": "0", "findings": [], "coverage": []})
            print("Documentation review preparation: " + preparation.status)
            return 0
        if args.mode == "worker":
            directory = Path(args.output)
            directory.mkdir(parents=True, exist_ok=True)
            preparation = load_preparation(Path(args.input), with_snapshot=True)
            report = run_worker(preparation, github, store, env, UrllibTransport())
            write_json(directory / "report.json", report)
            print("Documentation review: " + str(report["status"]))
            return 0
        directory = Path(args.directory)
        raw_plan = read_json(directory / "plan.json")
        if raw_plan["status"] in {"ignored", "superseded", "duplicate"}:
            return 0
        preparation = load_preparation(directory)
        if Path(args.report).exists():
            report = read_json(Path(args.report))
        elif preparation.status == "ready":
            # Caller must stop the container before using this recovery path.
            report = store.recover(preparation.owner)
            store.finish(preparation.trigger.pr, preparation.owner, report)
        else:
            report = {"status": preparation.status, "cost_rub": "0", "reserved_rub": "0"}
        report["head_sha"] = preparation.trigger.head_sha
        try:
            public_limit = str(run_limit(env.get("YDBDOC_REVIEW_MAX_RUN_COST_RUB")))
        except ReviewError:
            public_limit = "некорректен или не задан"
        report.setdefault("limit_rub", public_limit)
        github.publish(preparation.trigger.pr, preparation.trigger.head_sha, summary(report),
                       "success" if report["status"] == "clean" else "neutral")
        return 0
    except Exception as error:  # noqa: BLE001 - no event/file/secret data in public diagnostics.
        code = error.code if isinstance(error, ReviewError) else "infrastructure_error"
        if args.mode in {"prepare", "gate"} and preparation is not None:
            with suppress(Exception):
                write_json(Path(args.directory) / "plan.json", Preparation(
                    code, preparation.trigger, preparation.owner).plan())
                store.finish(preparation.trigger.pr, preparation.owner, {
                    "status": code, "cost_rub": "0", "reserved_rub": "0", "findings": [], "coverage": []})
        if code == "superseded" and args.mode in {"publish", "finalize"}:
            return 0
        print("Documentation review failed: " + code, file=sys.stderr)
        return 1
    finally:
        with suppress(Exception):
            sdk.close()


if __name__ == "__main__":
    raise SystemExit(main())
