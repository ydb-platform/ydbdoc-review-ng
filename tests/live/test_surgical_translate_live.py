"""Optional paid live smoke for surgical translation (deselected by default)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
@pytest.mark.timeout(300)
def test_surgical_translate_changelog_and_live_hunk() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        pytest.skip("set YDBDOC_LIVE=1 to authorize the paid live hunk call")
    env = os.environ.copy()
    env.setdefault("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "180")
    result = subprocess.run(
        [sys.executable, "scripts/probe_surgical_translate_live.py"],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        timeout=240,
        env=env,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output
    assert "PASS unique rewrite" in result.stdout
    assert "PASS live hunk" in result.stdout
