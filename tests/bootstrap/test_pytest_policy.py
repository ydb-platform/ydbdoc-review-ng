from __future__ import annotations

import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_markers_and_default_options_are_registered(pytestconfig: pytest.Config) -> None:
    markers = {line.split(":", 1)[0] for line in pytestconfig.getini("markers")}
    assert {"unit", "integration", "e2e", "live"} <= markers
    assert pytestconfig.getoption("timeout") == 30.0
    assert pytestconfig.getoption("timeout_method") == "signal"

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert "--strict-markers" in addopts
    assert "--disable-socket" in addopts
    assert "-m 'not live'" in addopts


def test_socket_api_is_blocked_in_test_process() -> None:
    with (
        pytest.warns(UserWarning, match=r"socket\.socket"),
        pytest.raises(SocketBlockedError, match=r"socket\.socket"),
    ):
        socket.socket()


def test_default_selection_excludes_live_tests(tmp_path: Path) -> None:
    canary = tmp_path / "test_policy_canary.py"
    canary.write_text(
        """
import pytest


@pytest.mark.unit
def test_unit_canary_passes():
    assert True


@pytest.mark.live
def test_live_canary_must_be_deselected():
    pytest.fail("live tests must not run by default")
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"), str(canary), "-q"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "1 deselected" in result.stdout


def test_timeout_plugin_interrupts_a_stalled_test() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/bootstrap/canaries/timeout_canary.py", "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )

    assert result.returncode == 1
    assert "Timeout" in result.stdout + result.stderr
