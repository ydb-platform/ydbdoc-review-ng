from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.live
def test_glossary_probe_against_yandex_cloud() -> None:
    if os.environ.get("YDBDOC_LIVE") != "1":
        pytest.skip("set YDBDOC_LIVE=1 to run the paid glossary probe")

    result = subprocess.run(
        [sys.executable, "scripts/run_glossary_probe.py"],
        cwd=Path(__file__).parents[2],
        check=False,
        text=True,
        capture_output=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    output = Path(result.stdout.strip())
    assert (output / "prompt.txt").is_file()
    assert (output / "response.md").is_file()
    assert (output / "restored.md").is_file()
