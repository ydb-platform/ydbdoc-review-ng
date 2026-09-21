from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_t017_f01_dispatch_uses_only_trusted_pinned_runtime_checkout() -> None:
    checkout_sha = "11bd71901bbe5b1630ceea73d27597364c9af683"
    setup_python_sha = "a26af69be951a213d495a4c3e4e4022e16d87065"
    for name in ("doc_translate.yml", "doc_verify.yml"):
        workflow = (ROOT / ".github" / "workflows" / name).read_text()
        assert f"uses: actions/checkout@{checkout_sha}" in workflow
        assert "ref: ${{ vars.YDBDOC_TRUSTED_RUNTIME_SHA }}" in workflow
        assert "persist-credentials: false" in workflow
        assert '[[ "$YDBDOC_TRUSTED_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]]' in workflow
        assert 'test "$(git rev-parse HEAD)" = "$YDBDOC_TRUSTED_RUNTIME_SHA"' in workflow
        assert workflow.index("Verify trusted runtime pin") < workflow.index(
            "uses: ./.github/actions/doc-review"
        )
        assert "uses: ./.github/actions/doc-review" in workflow
        assert "actions/checkout@v" not in workflow

    action = (ROOT / ".github" / "actions" / "doc-review" / "action.yml").read_text()
    assert f"uses: actions/setup-python@{setup_python_sha}" in action
    assert "actions/setup-python@v" not in action


def test_t017_r01_trusted_sha_is_validated_before_checkout_consumes_it() -> None:
    checkout_sha = "11bd71901bbe5b1630ceea73d27597364c9af683"
    valid_sha = "a" * 40
    for name, job_name in (
        ("doc_translate.yml", "doc_translate"),
        ("doc_verify.yml", "doc_verify"),
    ):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_bytes())
        steps = workflow["jobs"][job_name]["steps"]
        checkout_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("uses") == f"actions/checkout@{checkout_sha}"
        )
        guards = [
            step
            for step in steps[:checkout_index]
            if step.get("env", {}).get("YDBDOC_TRUSTED_RUNTIME_SHA")
            == "${{ vars.YDBDOC_TRUSTED_RUNTIME_SHA }}"
        ]
        assert len(guards) == 1
        guard = guards[0]
        assert guard["shell"] == "bash"
        script = guard["run"]
        for invalid in ("", "untrusted-branch"):
            rejected = subprocess.run(
                ["bash", "-c", script],
                env={"YDBDOC_TRUSTED_RUNTIME_SHA": invalid},
                check=False,
            )
            assert rejected.returncode != 0
        accepted = subprocess.run(
            ["bash", "-c", script],
            env={"YDBDOC_TRUSTED_RUNTIME_SHA": valid_sha},
            check=False,
        )
        assert accepted.returncode == 0
        assert steps[checkout_index]["with"] == {
            "ref": "${{ vars.YDBDOC_TRUSTED_RUNTIME_SHA }}",
            "persist-credentials": False,
        }
        post_checkout = steps[checkout_index + 1]
        assert (
            'test "$(git rev-parse HEAD)" = "$YDBDOC_TRUSTED_RUNTIME_SHA"' in post_checkout["run"]
        )


def test_external_composite_installs_the_repository_that_contains_the_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action_path = ROOT / ".github" / "actions" / "doc-review"
    action = yaml.safe_load((action_path / "action.yml").read_bytes())
    install_step = next(
        step for step in action["runs"]["steps"] if "python -m pip install" in step.get("run", "")
    )

    working_directory = install_step["working-directory"].replace(
        "${{ github.action_path }}", str(action_path)
    )
    monkeypatch.chdir(tmp_path)
    install_root = Path(working_directory).resolve()

    assert install_root == ROOT
    assert (install_root / "pyproject.toml").is_file()


def test_ci_has_read_only_offline_quality_contract() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "contents: read" in workflow
    assert "offline-quality:" in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "actions/checkout@v4" in workflow
    assert "actions/setup-python@v5" in workflow
    assert "python-version: '3.11'" in workflow
    assert "cache: pip" in workflow
    assert "PIP_DISABLE_PIP_VERSION_CHECK: '1'" in workflow
    assert "python scripts/run_with_timeout.py 120 python -m pip install -e '.[dev]'" in workflow
    assert (
        "python scripts/run_with_timeout.py 60 python -m ruff check src tests scripts" in workflow
    )
    assert "python scripts/run_with_timeout.py 60 python -m mypy src" in workflow
    assert (
        "python scripts/run_with_timeout.py 60 python -m pytest tests/bootstrap -q "
        "-m 'unit or integration' --timeout=30"
    ) in workflow
    assert "services:" not in workflow
    assert "secrets." not in workflow
    assert "live" not in workflow
