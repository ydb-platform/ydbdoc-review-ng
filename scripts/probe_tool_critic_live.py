#!/usr/bin/env python3
"""P2 live smoke: tool-using critic against real DeepSeek on a tiny --wait wait fixture.

Requires production-shaped credentials and explicit opt-in:

  export YDBDOC_LIVE=1
  export YANDEX_API_KEY=...
  export YANDEX_FOLDER_ID=...
  python scripts/probe_tool_critic_live.py

Exit 0 on success (at least one tool_calls round, session finishes, draft defect gone).
Does not invent secrets; does not write secrets to stdout.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from ydbdoc_review_ng.models import (
    AttemptResult,
    ExecutionConfig,
    ModelCallResult,
    ModelRequest,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.quality.critic import build_pr_critic_request
from ydbdoc_review_ng.quality.tool_critic import run_tool_critic_chunk

SOURCE_PATH = "ydb/docs/ru/concepts/probe_wait.md"
TARGET_PATH = "ydb/docs/en/concepts/probe_wait.md"
SOURCE = (
    "* `--wait` — дождаться завершения создания блобовницы; если опция не "
    "указана, команда завершается сразу.\n"
).encode()
DRAFT = (
    "* `--wait wait` — wait for blob depot creation to complete; if omitted, "
    "the command returns immediately.\n"
).encode()
GLOSSARY = {
    "ydb/docs/en/glossary.md": b"Blob depot: a tablet that stores blobs.\n",
}


def _require_live() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        raise SystemExit("Refusing: set YDBDOC_LIVE=1 for this paid probe.")


def _creds() -> YandexCredentials:
    api_key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder_id = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if not api_key or not folder_id:
        raise SystemExit(
            "Need production-shaped YANDEX_API_KEY + YANDEX_FOLDER_ID "
            "(map DOC_REVIEW aliases into those names if that is what the shell has)."
        )
    return YandexCredentials(api_key, folder_id)


def main() -> int:
    _require_live()
    credentials = _creds()
    model = (
        os.environ.get("YDBDOC_MODEL_CRITIC")
        or os.environ.get("YDBDOC_MODEL")
        or "deepseek-v4-flash"
    ).strip()
    timeout = float(os.environ.get("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "180"))
    attempts: list[AttemptResult] = []
    client = YandexOpenAIClient(
        credentials,
        UrllibTransport(),
        attempts.append,
        execution=ExecutionConfig(max_attempts=2),
        timeout_seconds=timeout,
    )

    tool_rounds = 0
    patch_rounds = 0

    def invoke(request: ModelRequest) -> ModelCallResult:
        nonlocal tool_rounds, patch_rounds
        result = client.invoke(request)
        if result.tool_calls:
            tool_rounds += 1
            if any(call.name == "apply_patch" for call in result.tool_calls):
                patch_rounds += 1
        return result

    initial = build_pr_critic_request(
        model=model,
        source_files={SOURCE_PATH: SOURCE},
        translated_files={TARGET_PATH: DRAFT},
        glossary_files=GLOSSARY,
    )
    environment: Mapping[str, str] = {
        "YDBDOC_CRITIC_MAX_TOOL_TURNS": os.environ.get("YDBDOC_CRITIC_MAX_TOOL_TURNS", "12"),
    }
    outcome = run_tool_critic_chunk(
        invoke,
        model=model,
        source_path=SOURCE_PATH,
        target_path=TARGET_PATH,
        source_bytes=SOURCE,
        draft_bytes=DRAFT,
        glossary_files=GLOSSARY,
        developer_prompt=initial.developer_prompt or "",
        user_prompt=initial.prompt,
        validate_files=lambda _files: None,
        environment=environment,
    )

    reviewed = b"" if outcome.reviewed_bytes is None else outcome.reviewed_bytes
    text = reviewed.decode("utf-8", errors="replace")
    print(f"ok={outcome.ok}")
    print(f"tool_rounds={tool_rounds}")
    print(f"patch_rounds={patch_rounds}")
    print(f"failure={None if outcome.failure_reason is None else outcome.failure_reason.value}")
    print(f"detail={outcome.detail!r}")
    print(f"reviewed_has_wait_wait={'--wait wait' in text}")
    print(f"reviewed_has_wait_flag={'`--wait`' in text or '--wait' in text}")
    print(f"model_attempts={len(attempts)}")
    if attempts:
        last = attempts[-1]
        usage = last.usage
        print(
            "last_usage="
            f"in={usage.input_tokens} out={usage.output_tokens} total={usage.total_tokens}"
        )

    if not outcome.ok or outcome.reviewed_bytes is None:
        print("FAIL: tool critic session did not finish with reviewed bytes", file=sys.stderr)
        return 1
    if tool_rounds < 1:
        print("FAIL: expected at least one tool_calls round", file=sys.stderr)
        return 1
    if patch_rounds < 1:
        print("FAIL: expected at least one apply_patch", file=sys.stderr)
        return 1
    if "--wait wait" in text:
        print("FAIL: residual '--wait wait' in reviewed bytes", file=sys.stderr)
        return 1
    if "`--wait`" not in text and "--wait" not in text:
        print("FAIL: reviewed bytes lost the --wait option", file=sys.stderr)
        return 1
    print("PASS: tool-using critic patched --wait wait via live DeepSeek")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
