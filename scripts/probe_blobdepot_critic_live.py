#!/usr/bin/env python3
"""Live BlobDepot critic+arbiter probe against production-shaped Yandex credentials.

Fetches the translator draft from translation PR #54924 (commit e59c027 by default),
paired RU from source PR tip, optional old EN from main as presentation reference,
then runs review_pr (tool-using critic + arbiter).

  export YDBDOC_LIVE=1
  export YANDEX_API_KEY=...   # or map from YANDEX_CLOUD_API_KEY_DOC_REVIEW
  export YANDEX_FOLDER_ID=... # or map from YANDEX_CLOUD_FOLDER_DOC_REVIEW
  export YDBDOC_CRITIC_MAX_TOOL_TURNS=32   # optional
  python scripts/probe_blobdepot_critic_live.py

Exit 0 on GREEN or YELLOW. Prints findings on RED. Cost-conscious: no CI publish.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from ydbdoc_review_ng.models import (
    AttemptResult,
    ExecutionConfig,
    ModelCallResult,
    ModelRequest,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict

REPO = "ydb-platform/ydb"
# Translator-only draft on #54924 (before critic soft-publish).
DEFAULT_DRAFT_REF = "e59c027093487cb4ae32d93554a12dbb43b2e05e"
DEFAULT_OLD_EN_REF = "main"

PAIRS = (
    (
        "ydb/docs/ru/core/maintenance/manual/blobdepot.md",
        "ydb/docs/en/core/maintenance/manual/blobdepot.md",
    ),
    (
        "ydb/docs/ru/core/maintenance/manual/blobdepot_decommit.md",
        "ydb/docs/en/core/maintenance/manual/blobdepot_decommit.md",
    ),
    (
        "ydb/docs/ru/core/maintenance/manual/index.md",
        "ydb/docs/en/core/maintenance/manual/index.md",
    ),
)
TOC_EN = "ydb/docs/en/core/maintenance/manual/toc_i.yaml"
TOC_RU = "ydb/docs/ru/core/maintenance/manual/toc_i.yaml"


def _map_shell_aliases() -> None:
    if not os.environ.get("YANDEX_API_KEY", "").strip():
        alias = os.environ.get("YANDEX_CLOUD_API_KEY_DOC_REVIEW", "").strip()
        if alias:
            os.environ["YANDEX_API_KEY"] = alias
    if not os.environ.get("YANDEX_FOLDER_ID", "").strip():
        alias = os.environ.get("YANDEX_CLOUD_FOLDER_DOC_REVIEW", "").strip()
        if alias:
            os.environ["YANDEX_FOLDER_ID"] = alias


def _require_live() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        raise SystemExit("Refusing: set YDBDOC_LIVE=1 for this paid probe.")
    if not os.environ.get("YANDEX_API_KEY", "").strip():
        raise SystemExit("Need YANDEX_API_KEY (map DOC_REVIEW alias if needed).")
    if not os.environ.get("YANDEX_FOLDER_ID", "").strip():
        raise SystemExit("Need YANDEX_FOLDER_ID (map DOC_REVIEW alias if needed).")


def _gh_file(path: str, ref: str) -> bytes:
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{REPO}/contents/{path}?ref={ref}",
            "--jq",
            ".content",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh api failed for {path}@{ref}: {proc.stderr.strip()}")
    raw = "".join(proc.stdout.split())
    if not raw or raw == "null":
        raise SystemExit(f"missing content for {path}@{ref}")
    return base64.b64decode(raw)


def _source_ref() -> str:
    pinned = os.environ.get("YDBDOC_BLOBDEPOT_SOURCE_REF", "").strip()
    if pinned:
        return pinned
    proc = subprocess.run(
        ["gh", "api", f"repos/{REPO}/pulls/50839", "--jq", ".head.sha"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _load_corpus() -> tuple[
    dict[str, bytes],
    dict[str, bytes | None],
    dict[str, bytes],
    dict[str, dict[str, str | None]],
]:
    draft_ref = os.environ.get("YDBDOC_BLOBDEPOT_DRAFT_REF", DEFAULT_DRAFT_REF).strip()
    source_ref = _source_ref()
    old_en_ref = os.environ.get("YDBDOC_BLOBDEPOT_OLD_EN_REF", DEFAULT_OLD_EN_REF).strip()
    print(f"source_ref={source_ref} draft_ref={draft_ref} old_en_ref={old_en_ref}", flush=True)

    source_files: dict[str, bytes] = {}
    translated: dict[str, bytes | None] = {}
    presentation: dict[str, bytes] = {}
    for ru, en in PAIRS:
        source_files[ru] = _gh_file(ru, source_ref)
        translated[en] = _gh_file(en, draft_ref)
        presentation[en] = _gh_file(en, old_en_ref)

    source_files[TOC_RU] = _gh_file(TOC_RU, source_ref)
    translated[TOC_EN] = _gh_file(TOC_EN, draft_ref)
    # Snapshots are keyed by SOURCE path (see repair._subset_toc_snapshots).
    toc_snapshots = {
        TOC_RU: {
            "before": None,
            "after": source_files[TOC_RU].decode("utf-8"),
        }
    }
    return source_files, translated, presentation, toc_snapshots


class _LiveExecutor:
    def __init__(self, client: YandexOpenAIClient) -> None:
        self._client = client
        self.tool_rounds = 0

    def invoke(self, request: ModelRequest) -> ModelCallResult:
        result = self._client.invoke(request)
        if result.tool_calls:
            self.tool_rounds += 1
        role = request.role.value if hasattr(request.role, "value") else str(request.role)
        status = "tools" if result.tool_calls else ("fail" if result.failure else "text")
        print(f"invoke role={role} status={status}", flush=True)
        return result


def main() -> int:
    _map_shell_aliases()
    _require_live()
    os.environ.setdefault("YDBDOC_CRITIC_MAX_TOOL_TURNS", "32")

    source_files, translated, presentation, toc_snapshots = _load_corpus()
    print(
        f"pairs={len(PAIRS)} toc=1 draft_bytes="
        f"{sum(len(v or b'') for v in translated.values())}",
        flush=True,
    )

    credentials = YandexCredentials(
        os.environ["YANDEX_API_KEY"].strip(),
        os.environ["YANDEX_FOLDER_ID"].strip(),
    )
    model = (
        os.environ.get("YDBDOC_MODEL_CRITIC")
        or os.environ.get("YDBDOC_MODEL")
        or "deepseek-v4-flash"
    ).strip()
    arbiter_model = (os.environ.get("YDBDOC_MODEL_ARBITER") or model).strip()
    timeout = float(os.environ.get("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "600"))
    attempts: list[AttemptResult] = []
    client = YandexOpenAIClient(
        credentials,
        UrllibTransport(),
        attempts.append,
        execution=ExecutionConfig(max_attempts=2),
        timeout_seconds=timeout,
    )
    executor = _LiveExecutor(client)

    corrected, final = review_pr(
        executor,
        critic_model=model,
        arbiter_model=arbiter_model,
        source_files=source_files,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda _files: None,
        presentation_reference_files=presentation,
        toc_snapshots=toc_snapshots,
    )

    print(f"verdict={final.verdict.value}", flush=True)
    print(f"tool_rounds={executor.tool_rounds}", flush=True)
    print(f"model_attempts={len(attempts)}", flush=True)
    print(f"findings={len(final.findings)}", flush=True)
    for item in final.findings:
        print(
            f"- {item.target_path}:{item.target_line} "
            f"{item.reason!r} / {item.expected_correction!r} "
            f"snippet={item.searchable_snippet!r}",
            flush=True,
        )

    out = Path(
        os.environ.get("YDBDOC_BLOBDEPOT_OUT", "artifacts/blobdepot_critic_live.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    reviewed_dir = out.parent / "blobdepot_reviewed"
    reviewed_dir.mkdir(parents=True, exist_ok=True)
    for path, content in corrected.items():
        target = reviewed_dir / Path(path).name
        target.write_bytes(content)
    payload = {
        "verdict": final.verdict.value,
        "tool_rounds": executor.tool_rounds,
        "attempts": len(attempts),
        "findings": [
            {
                "path": item.target_path,
                "line": item.target_line,
                "reason": item.reason,
                "correction": item.expected_correction,
                "snippet": item.searchable_snippet,
            }
            for item in final.findings
        ],
        "corrected_paths": sorted(corrected),
        "reviewed_dir": str(reviewed_dir),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}", flush=True)
    print(f"reviewed_dir={reviewed_dir}", flush=True)

    if final.verdict in {Verdict.GREEN, Verdict.YELLOW}:
        print(f"PASS: BlobDepot critic+arbiter → {final.verdict.value}")
        return 0
    print(f"FAIL: verdict={final.verdict.value}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
