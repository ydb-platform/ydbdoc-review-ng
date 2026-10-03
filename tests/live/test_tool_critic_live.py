"""Optional paid live smoke for the tool-using critic (deselected by default)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
@pytest.mark.timeout(900)
def test_tool_critic_patches_wait_wait_against_deepseek() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        pytest.skip("set YDBDOC_LIVE=1 to authorize the paid live model calls")
    if not os.environ.get("YANDEX_API_KEY", "").strip():
        pytest.skip("YANDEX_API_KEY required for production-shaped live critic smoke")
    if not os.environ.get("YANDEX_FOLDER_ID", "").strip():
        pytest.skip("YANDEX_FOLDER_ID required for production-shaped live critic smoke")

    env = os.environ.copy()
    env.setdefault("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "180")
    env.setdefault("YDBDOC_CRITIC_MAX_TOOL_TURNS", "32")
    result = subprocess.run(
        [sys.executable, "scripts/probe_tool_critic_live.py"],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        timeout=840,
        env=env,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output
    assert "tool_rounds=" in result.stdout
    assert "PASS:" in result.stdout
