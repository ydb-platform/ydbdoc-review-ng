"""Optional paid live e2e: #50858 delta-scoped DeepSeek critic + arbiter."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.timeout(1800)
def test_pr_50858_delta_scoped_critic_arbiter_against_deepseek() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        pytest.skip("set YDBDOC_LIVE=1 to authorize the paid live pipeline call")
    env = os.environ.copy()
    if not env.get("YANDEX_API_KEY", "").strip():
        alias = env.get("YANDEX_CLOUD_API_KEY_DOC_REVIEW", "").strip()
        if alias:
            env["YANDEX_API_KEY"] = alias
    if not env.get("YANDEX_FOLDER_ID", "").strip():
        alias = env.get("YANDEX_CLOUD_FOLDER_DOC_REVIEW", "").strip()
        if alias:
            env["YANDEX_FOLDER_ID"] = alias
    if not env.get("GH_TOKEN", "").strip():
        token = env.get("YDB_GH_TOKEN", "").strip()
        if token:
            env["GH_TOKEN"] = token
    if not env.get("YANDEX_API_KEY", "").strip() or not env.get("YANDEX_FOLDER_ID", "").strip():
        pytest.skip("Yandex credentials required for live DeepSeek pipeline")
    env.setdefault("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "180")
    env.setdefault("YDBDOC_CRITIC_MAX_TOOL_TURNS", "16")
    result = subprocess.run(
        [sys.executable, "scripts/probe_pr_50858_delta_review_live.py"],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        timeout=1740,
        env=env,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output
    assert "PASS 50858 delta-scoped critic+arbiter GREEN" in result.stdout
    assert "critic_calls=" in result.stdout
    assert "arbiter_calls=" in result.stdout
