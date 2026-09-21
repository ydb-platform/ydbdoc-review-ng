from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "run_with_timeout.py"
TREE = ROOT / "tests" / "bootstrap" / "helpers" / "process_tree.py"


def wrapper(*args: str, timeout: float = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WRAPPER), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def wait_until(predicate: Callable[[], bool], seconds: float = 3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before deadline")


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def cleanup_processes(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def read_pids(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [int(value) for value in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("code", [0, 17])
def test_normal_exit_is_preserved(code: int) -> None:
    result = wrapper("2", sys.executable, "-c", f"raise SystemExit({code})")
    assert result.returncode == code


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf", "invalid"])
def test_invalid_timeout_does_not_launch_child(tmp_path: Path, seconds: str) -> None:
    marker = tmp_path / "launched"
    result = wrapper(seconds, sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip()
    assert not marker.exists()


def test_missing_command_is_usage_error() -> None:
    result = wrapper("1")
    assert result.returncode == 2
    assert result.stderr.strip()


def test_missing_executable_returns_127() -> None:
    result = wrapper("1", "definitely-not-a-real-ydbdoc-command")
    assert result.returncode == 127


def test_non_executable_returns_126(tmp_path: Path) -> None:
    command = tmp_path / "not-executable"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o644)
    result = wrapper("1", str(command))
    assert result.returncode == 126


def test_negative_signal_exit_is_mapped_to_shell_status() -> None:
    code = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    result = wrapper("2", sys.executable, "-c", code)
    assert result.returncode == 143


def test_timeout_kills_term_ignoring_process_group(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    pid_file = tmp_path / "pids"
    pids: list[int] = []
    started = time.monotonic()
    try:
        result = wrapper(
            "1",
            sys.executable,
            str(TREE),
            str(ready),
            str(pid_file),
            "--ignore-term",
            timeout=6,
        )
        pids = read_pids(pid_file)
        assert ready.exists()
        assert result.returncode == 124
        elapsed = time.monotonic() - started
        assert 1.35 <= elapsed < 5
        assert "1" in result.stderr
        assert str(TREE) in result.stderr
        wait_until(lambda: all(not pid_exists(pid) for pid in pids))
    finally:
        pids = list({*pids, *read_pids(pid_file)})
        cleanup_processes(pids)


def test_timeout_diagnostic_does_not_dump_environment(tmp_path: Path) -> None:
    secret = "not-for-timeout-diagnostics"
    environment = os.environ.copy()
    environment["YDBDOC_TEST_SECRET"] = secret
    result = subprocess.run(
        [sys.executable, str(WRAPPER), "0.05", sys.executable, "-c", "import time; time.sleep(2)"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=4,
        check=False,
    )

    assert result.returncode == 124
    assert secret not in result.stderr


def test_sigterm_is_forwarded_and_group_is_cleaned(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    pid_file = tmp_path / "pids"
    pids: list[int] = []
    process = subprocess.Popen(
        [
            sys.executable,
            str(WRAPPER),
            "20",
            sys.executable,
            str(TREE),
            str(ready),
            str(pid_file),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_until(ready.exists)
        pids = read_pids(pid_file)
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=6)
        assert process.returncode == 143, stderr
        wait_until(lambda: all(not pid_exists(pid) for pid in pids))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        pids = list({*pids, *read_pids(pid_file)})
        cleanup_processes(pids)


def test_sigterm_during_launch_handoff_cleans_group_and_returns_143(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    pid_file = tmp_path / "pids"
    harness = tmp_path / "launch_handoff.py"
    harness.write_text(
        """
from __future__ import annotations

import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

wrapper_path = Path(sys.argv[1])
ready = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("run_with_timeout", wrapper_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_popen = module.subprocess.Popen


def launch_during_handoff(command: list[str], **kwargs: object) -> object:
    child = real_popen(command, **kwargs)
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        child.kill()
        child.wait()
        raise RuntimeError("child process group did not become ready")
    os.kill(os.getpid(), signal.SIGTERM)
    return child


module.subprocess.Popen = launch_during_handoff
sys.argv = [str(wrapper_path), *sys.argv[3:]]
raise SystemExit(module.main())
""",
        encoding="utf-8",
    )
    pids: list[int] = []
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(harness),
                str(WRAPPER),
                str(ready),
                "20",
                sys.executable,
                str(TREE),
                str(ready),
                str(pid_file),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        pids = read_pids(pid_file)
        assert len(pids) == 2
        assert result.returncode == 143
        wait_until(lambda: all(not pid_exists(pid) for pid in pids))
    finally:
        pids = list({*pids, *read_pids(pid_file)})
        cleanup_processes(pids)
