#!/usr/bin/env python3
"""Live e2e: #50858 unique-dest + DeepSeek critic/arbiter scoped to PR delta.

DeepSeek always runs. Task is chat-like: judge only the source delta and
regressions vs previous EN. Findings outside touched EN lines are dropped.

No GitHub publish. Set YDBDOC_LIVE=1 and Yandex credentials.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ydbdoc_review_ng.models import (
    AttemptResult,
    ExecutionConfig,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Finding, Verdict
from ydbdoc_review_ng.translation.surgical import SurgicalMode, plan_surgical_update

REPO = "ydb-platform/ydb"
OLD = b"./maintenance/manual/dynamic-config"
NEW = b"./devops/configuration-management/configuration-v1/dynamic-config"
SIBLING = b"./maintenance/manual/virtual_storage_groups_decommit.md"
CHANGELOGS = (
    "ydb/docs/ru/core/changelog-enterprise.md",
    "ydb/docs/ru/core/changelog-server.md",
)
def _map_aliases() -> None:
    if not os.environ.get("YANDEX_API_KEY", "").strip():
        alias = os.environ.get("YANDEX_CLOUD_API_KEY_DOC_REVIEW", "").strip()
        if alias:
            os.environ["YANDEX_API_KEY"] = alias
    if not os.environ.get("YANDEX_FOLDER_ID", "").strip():
        alias = os.environ.get("YANDEX_CLOUD_FOLDER_DOC_REVIEW", "").strip()
        if alias:
            os.environ["YANDEX_FOLDER_ID"] = alias
    if not os.environ.get("GH_TOKEN", "").strip():
        token = os.environ.get("YDB_GH_TOKEN", "").strip()
        if token:
            os.environ["GH_TOKEN"] = token


def _fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def _gh(path: str) -> str:
    proc = subprocess.run(["gh", "api", path], check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        _fail(proc.stderr.strip() or proc.stdout.strip() or path)
    return proc.stdout


def _file(path: str, ref: str) -> bytes:
    raw = json.loads(_gh(f"repos/{REPO}/contents/{path}?ref={ref}"))["content"]
    return base64.b64decode("".join(raw.split()))


def _changed_ru_items(before: bytes, after: bytes) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for left, right in zip(before.splitlines(), after.splitlines(), strict=True):
        if left == right:
            continue
        items.append((left.decode("utf-8"), right.decode("utf-8")))
    return items


def _touched_en_lines(draft: bytes, new_dest: bytes) -> list[int]:
    lines: list[int] = []
    for index, line in enumerate(draft.splitlines(), start=1):
        if new_dest in line or OLD in line:
            lines.append(index)
    return lines


def _delta_brief(
    *,
    ru_path: str,
    en_path: str,
    items: list[tuple[str, str]],
    touched: list[int],
) -> str:
    blocks = [
        "CHANGE CLASS: unique_dest",
        "Work like a chat editor reviewing one documentation PR.",
        "Judge ONLY the source PR delta below and regressions vs previous EN.",
        "Do NOT audit historical changelog sections outside the delta.",
        (
            "Do NOT invent style findings (periods, articles, old issue numbers) "
            "outside touched EN lines."
        ),
        f"Source: {ru_path}",
        f"Target: {en_path}",
        f"Touched EN line numbers (1-based): {touched}",
        "Source delta (RU before → RU after):",
    ]
    for index, (before, after) in enumerate(items, start=1):
        blocks.append(f"\n### delta {index}\nRU before:\n{before}\n\nRU after:\n{after}")
    blocks.append(
        "\nCritic: if draft already contains the new destinations from RU after "
        "and previous EN wording is intact, finish without patches. "
        "Patch only defects inside touched lines."
    )
    blocks.append(
        "\nArbiter: default GREEN. Emit a finding only if the delta is missing/"
        "wrong or previous EN outside the delta was damaged. "
        "searchable_snippet must lie on a touched EN line."
    )
    return "\n".join(blocks)


def _line_in_touched(line: int | None, touched: list[int], *, pad: int = 2) -> bool:
    if line is None:
        return False
    allowed = set()
    for item in touched:
        allowed.update(range(max(1, item - pad), item + pad + 1))
    return line in allowed


def _filter_findings(
    findings: tuple[Finding, ...],
    touched_by_path: dict[str, list[int]],
) -> tuple[Finding, ...]:
    kept: list[Finding] = []
    for finding in findings:
        touched = touched_by_path.get(finding.target_path, [])
        if not touched:
            kept.append(finding)
            continue
        if finding.searchable_snippet is None:
            kept.append(finding)
            continue
        if _line_in_touched(finding.target_line, touched):
            kept.append(finding)
            continue
        print(
            f"drop out-of-delta finding {finding.target_path} "
            f"line={finding.target_line}: {finding.reason[:120]!r}",
            flush=True,
        )
    return tuple(kept)


def _mangled(text: bytes) -> str | None:
    if b".sys/`top_queries_" in text or b".sys/`top_queries" in text:
        return "presentation-map style backtick around .sys/top_queries"
    if text.count(b"`YDB`") > 8:
        return "excess `YDB` wrapping"
    return None


def main() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        raise SystemExit("set YDBDOC_LIVE=1")
    _map_aliases()
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if not key or not folder:
        raise SystemExit("need YANDEX_API_KEY and YANDEX_FOLDER_ID")

    pr = json.loads(_gh(f"repos/{REPO}/pulls/50858"))
    merge = pr["merge_commit_sha"]
    parent = json.loads(_gh(f"repos/{REPO}/commits/{merge}"))["parents"][0]["sha"]
    print(f"50858 parent={parent[:12]} merge={merge[:12]}", flush=True)

    source_files: dict[str, bytes] = {}
    translated: dict[str, bytes] = {}
    previous_en: dict[str, bytes] = {}
    briefs: list[str] = []
    touched_by_path: dict[str, list[int]] = {}

    for ru_path in CHANGELOGS:
        en_path = ru_path.replace("/ru/", "/en/")
        before = _file(ru_path, parent)
        after = _file(ru_path, merge)
        existing = _file(en_path, "main")
        plan = plan_surgical_update(before, after, existing)
        print(f"translate {en_path} mode={plan.mode.value}", flush=True)
        if plan.mode is not SurgicalMode.UNIQUE_REPLACEMENTS or plan.patched_target is None:
            _fail(f"expected unique_replacements for {en_path}")
        patched = plan.patched_target
        if OLD in patched or NEW not in patched:
            _fail(f"dest rewrite incomplete for {en_path}")
        if en_path.endswith("changelog-server.md") and SIBLING not in patched:
            _fail("sibling virtual_storage dest was rewritten")
        damage = _mangled(patched)
        if damage:
            _fail(f"translator mangled {en_path}: {damage}")
        items = _changed_ru_items(before, after)
        touched = _touched_en_lines(patched, NEW)
        if not items or not touched:
            _fail(f"empty delta metadata for {en_path}")
        print(f"delta items={len(items)} touched_en_lines={touched}", flush=True)
        source_files[ru_path] = after
        translated[en_path] = patched
        previous_en[en_path] = existing
        touched_by_path[en_path] = touched
        briefs.append(
            _delta_brief(ru_path=ru_path, en_path=en_path, items=items, touched=touched)
        )

    operator = (
        "DeepSeek critic and arbiter MUST stay inside the PR delta.\n\n"
        + "\n\n====\n\n".join(briefs)
    )

    out_dir = ROOT / "artifacts" / "pr_50858_delta_review"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "operator_context.md").write_text(operator, encoding="utf-8")
    for path, content in translated.items():
        (out_dir / Path(path).name).write_bytes(content)

    timeout = float(os.environ.get("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "180"))
    os.environ.setdefault("YDBDOC_CRITIC_MAX_TOOL_TURNS", "16")
    recorded: list[AttemptResult] = []
    critic_calls = 0
    arbiter_calls = 0

    def persist(attempt: AttemptResult) -> None:
        nonlocal critic_calls, arbiter_calls
        recorded.append(attempt)
        role = attempt.request_role.value
        if role == "critic":
            critic_calls += 1
        elif role == "arbiter":
            arbiter_calls += 1
        print(
            f"model role={role} status={attempt.status.value} "
            f"http={attempt.http_status} in={attempt.usage.input_tokens} "
            f"out={attempt.usage.output_tokens} status_field={attempt.response_status}",
            flush=True,
        )

    client = YandexOpenAIClient(
        YandexCredentials(key, folder),
        UrllibTransport(),
        persist,
        execution=ExecutionConfig(max_attempts=2),
        timeout_seconds=timeout,
    )
    print("review_pr with delta brief; DeepSeek always; filter out-of-delta findings", flush=True)
    corrected, result = review_pr(
        client,
        critic_model="deepseek-v4-flash",
        arbiter_model="deepseek-v4-flash",
        source_files=source_files,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda _files: None,
        operator_context=operator,
        presentation_reference_files=previous_en,
    )
    if critic_calls < 1 or arbiter_calls < 1:
        _fail(f"DeepSeek must run: critic_calls={critic_calls} arbiter_calls={arbiter_calls}")

    for path, content in corrected.items():
        (out_dir / (Path(path).name + ".reviewed.md")).write_bytes(content)
        if content != translated[path]:
            print(f"critic changed bytes: {path}", flush=True)
        damage = _mangled(content)
        if damage:
            _fail(f"critic mangled {path}: {damage}")
        if OLD in content or NEW not in content:
            _fail(f"dest contract broken in {path}")
        if path.endswith("changelog-server.md") and SIBLING not in content:
            _fail("sibling virtual_storage dest missing after critic")

    raw_findings = result.findings
    kept = _filter_findings(raw_findings, touched_by_path)
    verdict = result.verdict
    if kept != raw_findings:
        verdict = Verdict.RED if kept else Verdict.GREEN
        if kept and all(not f.repairable for f in kept):
            # Keep YELLOW if remaining findings look minor; review_pr already ranked.
            if result.verdict is Verdict.YELLOW:
                verdict = Verdict.YELLOW
            elif any("missing" in f.reason.lower() for f in kept):
                verdict = Verdict.RED
            else:
                verdict = Verdict.YELLOW

    print(
        f"verdict_raw={result.verdict.value} findings_raw={len(raw_findings)} "
        f"verdict_delta={verdict.value} findings_delta={len(kept)} "
        f"critic_calls={critic_calls} arbiter_calls={arbiter_calls}",
        flush=True,
    )
    for finding in kept:
        print(
            f"- KEEP {finding.target_path} line={finding.target_line} {finding.reason!r}",
            flush=True,
        )
    (out_dir / "verdict.txt").write_text(
        f"raw={result.verdict.value} delta={verdict.value}\n"
        + "\n".join(f.reason for f in kept),
        encoding="utf-8",
    )
    if verdict is Verdict.RED:
        _fail(f"delta-scoped arbiter {verdict.value}")
    if verdict is not Verdict.GREEN:
        _fail(f"expected GREEN after delta scope, got {verdict.value}")
    print("PASS 50858 delta-scoped critic+arbiter GREEN", flush=True)


if __name__ == "__main__":
    main()
