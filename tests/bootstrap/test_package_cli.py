from __future__ import annotations

import importlib.metadata
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(Path(sys.executable).with_name("ydbdoc-review")), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_distribution_metadata_and_src_package_discovery() -> None:
    distribution = importlib.metadata.distribution("ydbdoc-review-ng")
    assert distribution.version == "1.0.1"
    entry_points = importlib.metadata.entry_points(group="console_scripts")
    matching = [entry.value for entry in entry_points if entry.name == "ydbdoc-review"]
    assert matching == ["ydbdoc_review_ng.cli:main"]
    assert distribution.read_text("top_level.txt") == "ydbdoc_review_ng\n"


def test_package_manifest_has_the_fixed_minimal_contract() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["build-system"] == {
        "requires": ["setuptools>=69"],
        "build-backend": "setuptools.build_meta",
    }
    assert config["project"]["name"] == "ydbdoc-review-ng"
    assert config["project"]["requires-python"] == ">=3.11"
    assert config["project"]["dependencies"] == ["PyYAML>=6,<7"]
    assert config["project"]["optional-dependencies"] == {
        "runtime": ["ydb[yc]>=3.31,<4"],
        "dev": [
            "pytest>=8",
            "pytest-timeout>=2.3",
            "pytest-socket>=0.7",
            "ruff>=0.11",
            "mypy>=1.15",
        ],
    }


def test_cli_help_is_successful_on_stdout() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "{translate,verify}" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("args", [(), ("unknown",), ("continue",)])
def test_cli_rejects_missing_or_unknown_mode(args: tuple[str, ...]) -> None:
    result = run_cli(*args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr


@pytest.mark.parametrize("mode", ["translate", "verify"])
def test_valid_modes_require_explicit_workflow_inputs(mode: str) -> None:
    result = run_cli(mode)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "required" in result.stderr


@pytest.mark.parametrize("argv", [["translate", "--help"], ["verify", "--help"], ["--help"]])
def test_import_help_and_modes_avoid_application_side_effects(argv: list[str]) -> None:
    probe = r"""
import builtins
import io
import os
import pathlib
import socket
import subprocess
import sys

def forbidden(*args, **kwargs):
    raise AssertionError("application side effect attempted")

class ForbiddenInput:
    def read(self, *args, **kwargs):
        return forbidden(*args, **kwargs)
    def readline(self, *args, **kwargs):
        return forbidden(*args, **kwargs)

original_environment = os.environ

class TrackingEnvironment:
    def check_caller(self):
        caller = sys._getframe(2).f_code.co_filename
        if "ydbdoc_review_ng" in caller:
            forbidden(caller)
    def __getitem__(self, key):
        self.check_caller()
        return original_environment[key]
    def get(self, key, default=None):
        self.check_caller()
        return original_environment.get(key, default)
    def __iter__(self):
        self.check_caller()
        return iter(original_environment)
    def __len__(self):
        self.check_caller()
        return len(original_environment)

builtins.open = forbidden
pathlib.Path.open = forbidden
pathlib.Path.read_text = forbidden
pathlib.Path.read_bytes = forbidden
socket.socket = forbidden
subprocess.Popen = forbidden
os.getenv = forbidden
os.environ = TrackingEnvironment()
sys.stdin = ForbiddenInput()

from ydbdoc_review_ng import cli

sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    result = cli.main(ARGV)
except SystemExit as error:
    result = error.code
if result != EXPECTED:
    raise AssertionError((result, sys.stdout.getvalue(), sys.stderr.getvalue()))
""".replace("ARGV", repr(argv)).replace("EXPECTED", "0")

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
