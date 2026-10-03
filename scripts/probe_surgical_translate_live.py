#!/usr/bin/env python3
"""Live/offline probe for surgical translation.

1. Reconstruct PR #50858 changelog deltas against current EN on main (no model).
2. If YDBDOC_LIVE=1, translate one prose hunk with DeepSeek and stitch it.

  export YDBDOC_LIVE=1
  export YANDEX_API_KEY=...   # or YANDEX_CLOUD_API_KEY_DOC_REVIEW
  export YANDEX_FOLDER_ID=... # or YANDEX_CLOUD_FOLDER_DOC_REVIEW
  python scripts/probe_surgical_translate_live.py
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ydbdoc_review_ng.domain import (  # noqa: E402
    FilePair,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import PairKey  # noqa: E402
from ydbdoc_review_ng.models import (  # noqa: E402
    AttemptResult,
    ExecutionConfig,
    ModelCallResult,
    ModelRequest,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.parser.markdown import build_markdown_plan  # noqa: E402
from ydbdoc_review_ng.runtime_content import Document, RuntimeContent  # noqa: E402
from ydbdoc_review_ng.scope import FileOperation, ScopeEntry, ScopeOrigin  # noqa: E402
from ydbdoc_review_ng.translation import build_translation_request  # noqa: E402
from ydbdoc_review_ng.translation.surgical import SurgicalMode, plan_surgical_update  # noqa: E402

REPO = "ydb-platform/ydb"
OLD = b"./maintenance/manual/dynamic-config"
NEW = b"./devops/configuration-management/configuration-v1/dynamic-config"
CHANGELOGS = (
    "ydb/docs/ru/core/changelog-enterprise.md",
    "ydb/docs/ru/core/changelog-server.md",
)


def _map_shell_aliases() -> None:
    if not os.environ.get("YANDEX_API_KEY", "").strip():
        alias = os.environ.get("YANDEX_CLOUD_API_KEY_DOC_REVIEW", "").strip()
        if alias:
            os.environ["YANDEX_API_KEY"] = alias
    if not os.environ.get("YANDEX_FOLDER_ID", "").strip():
        alias = os.environ.get("YANDEX_CLOUD_FOLDER_DOC_REVIEW", "").strip()
        if alias:
            os.environ["YANDEX_FOLDER_ID"] = alias


def _gh_json(path: str) -> str:
    proc = subprocess.run(
        ["gh", "api", path],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def _gh_file(path: str, ref: str) -> bytes:
    proc = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/{path}?ref={ref}", "--jq", ".content"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh api failed for {path}@{ref}: {proc.stderr.strip()}")
    raw = "".join(proc.stdout.split())
    return base64.b64decode(raw)


def _probe_50858() -> None:
    import json

    pr = json.loads(_gh_json(f"repos/{REPO}/pulls/50858"))
    merge = pr["merge_commit_sha"]
    parent = subprocess.run(
        ["gh", "api", f"repos/{REPO}/commits/{merge}", "--jq", ".parents[0].sha"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"50858 merge={merge[:12]} parent={parent[:12]}", flush=True)
    for ru_path in CHANGELOGS:
        en_path = ru_path.replace("/ru/", "/en/")
        before = _gh_file(ru_path, parent)
        after = _gh_file(ru_path, merge)
        existing = _gh_file(en_path, "main")
        plan = plan_surgical_update(before, after, existing)
        print(
            f"{ru_path} mode={plan.mode.value} "
            f"before={len(before)} after={len(after)} en={len(existing)}",
            flush=True,
        )
        if plan.mode is not SurgicalMode.UNIQUE_REPLACEMENTS or plan.patched_target is None:
            raise SystemExit(f"FAIL: expected unique_replacements for {ru_path}")
        patched = plan.patched_target
        if OLD in patched and NEW not in patched:
            raise SystemExit(f"FAIL: URL rewrite incomplete for {en_path}")
        print(
            f"PASS unique rewrite {en_path} "
            f"old={patched.count(OLD)} new={patched.count(NEW)}",
            flush=True,
        )


class _GithubBefore:
    def __init__(self, before: bytes) -> None:
        self.before = before

    def read_bytes(self, snapshot, path):
        return self.before


class _SourceBefore:
    def __init__(self, before: bytes) -> None:
        self.source_base_snapshot = SnapshotRef(
            RepositoryId("ydb-platform/ydb"), GitSha("b" * 40)
        )
        self.github = _GithubBefore(before)


class _LiveModels:
    def __init__(self, client: YandexOpenAIClient) -> None:
        self.client = client
        self.calls: list[ModelRequest] = []
        self.cost = Decimal(0)

    def invoke(self, request: ModelRequest) -> ModelCallResult:
        self.calls.append(request)
        print(f"live prompt_chars={len(request.prompt)}", flush=True)
        if len(request.prompt) > 8000:
            raise SystemExit("FAIL: hunk prompt still too large")
        return self.client.invoke(request)


def _probe_live_hunk() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        print("SKIP live hunk (set YDBDOC_LIVE=1)", flush=True)
        return
    _map_shell_aliases()
    if not os.environ.get("YANDEX_API_KEY", "").strip() or not os.environ.get(
        "YANDEX_FOLDER_ID", ""
    ).strip():
        raise SystemExit("Need YANDEX_API_KEY and YANDEX_FOLDER_ID for live hunk")
    timeout = float(os.environ.get("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "180"))
    recorded: list[AttemptResult] = []

    def persist(attempt: AttemptResult, **_kwargs) -> None:
        recorded.append(attempt)

    client = YandexOpenAIClient(
        YandexCredentials(
            os.environ["YANDEX_API_KEY"],
            os.environ["YANDEX_FOLDER_ID"],
        ),
        UrllibTransport(),
        persist,
        execution=ExecutionConfig(max_attempts=2),
        timeout_seconds=timeout,
    )
    old_url = b"./maintenance/manual/dynamic-config#x"
    new_url = b"./devops/configuration-management/configuration-v1/dynamic-config#x"
    source_before = (
        "* Старая формулировка для [конфигурации]("
        + old_url.decode()
        + ").\n* Этот пункт не меняется.\n"
    ).encode()
    source_after = (
        "* Новая формулировка для [конфигурации]("
        + new_url.decode()
        + ").\n* Этот пункт не меняется.\n"
    ).encode()
    existing_en = (
        b"* Previous English for [configuration](" + old_url + b").\n"
        b"* This item stays.\n"
    )
    source_path = RepoPath("ydb/docs/ru/core/page.md")
    target_path = RepoPath("ydb/docs/en/core/page.md")
    entry = ScopeEntry(
        FilePair(Locale.RU, Locale.EN, source_path, target_path),
        source_after,
        existing_en,
        ScopeOrigin.INITIAL,
        FileOperation.TRANSLATE,
        (PairKey(RepoPath("page.md")),),
        None,
        None,
    )
    plan = build_markdown_plan(
        SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40)),
        source_path,
        source_after,
    )
    document = Document(
        entry, source_after, plan, build_translation_request(source_after, plan)
    )
    models = _LiveModels(client)
    content = RuntimeContent(_SourceBefore(source_before), models, os.environ)
    _accepted, translated = content._translate_document(document)
    text = translated.translated_markdown
    print(repr(text), flush=True)
    if "* This item stays." not in text:
        raise SystemExit("FAIL: untouched English line was rewritten")
    if "Previous English" in text:
        raise SystemExit("FAIL: stale English hunk remained")
    if new_url.decode() not in text:
        raise SystemExit("FAIL: new URL missing from stitched English")
    if not models.calls:
        raise SystemExit("FAIL: expected a live hunk model call")
    print(f"PASS live hunk calls={len(models.calls)}", flush=True)


def main() -> None:
    _probe_50858()
    _probe_live_hunk()
    print("PASS: surgical translation probe", flush=True)


if __name__ == "__main__":
    main()
