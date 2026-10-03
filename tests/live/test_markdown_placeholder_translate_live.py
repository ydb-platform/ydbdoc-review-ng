"""Optional paid live smoke: Markdown-out translation with placeholders."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.timeout(300)
def test_markdown_placeholder_translate_against_deepseek() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        pytest.skip("set YDBDOC_LIVE=1 to authorize the paid live translate call")
    env = os.environ.copy()
    if not env.get("YANDEX_API_KEY", "").strip():
        alias = env.get("YANDEX_CLOUD_API_KEY_DOC_REVIEW", "").strip()
        if alias:
            env["YANDEX_API_KEY"] = alias
    if not env.get("YANDEX_FOLDER_ID", "").strip():
        alias = env.get("YANDEX_CLOUD_FOLDER_DOC_REVIEW", "").strip()
        if alias:
            env["YANDEX_FOLDER_ID"] = alias
    if not env.get("YANDEX_API_KEY", "").strip() or not env.get("YANDEX_FOLDER_ID", "").strip():
        pytest.skip("Yandex credentials required for live DeepSeek translate")
    env.setdefault("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "180")
    result = subprocess.run(
        [sys.executable, "scripts/probe_markdown_placeholder_translate_live.py"],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output
    assert "PASS markdown placeholder translate" in result.stdout
    assert "schema=None" in result.stdout
