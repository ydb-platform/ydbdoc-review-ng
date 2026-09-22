from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def run_action_inputs(mode, *, source="", target="", budget="", pr="42"):
    action = yaml.safe_load((ROOT / ".github/actions/doc-review/action.yml").read_bytes())
    steps = action["runs"]["steps"]
    guards = [step for step in steps if step.get("name") == "Validate mode inputs"]
    assert len(guards) == 1, "action must validate inputs before Python setup/install"
    assert steps[0] == guards[0]
    assert action["inputs"]["source-sha"]["required"] is False
    script = (
        guards[0]["run"]
        + "\n"
        + next(step["run"] for step in steps if "-m ydbdoc_review_ng.cli" in step.get("run", ""))
    )
    return subprocess.run(
        ["bash", "-euc", 'python() { printf "%s\\n" "$@"; };\n' + script],
        env={
            "MODE": mode,
            "PR": pr,
            "SOURCE_SHA": source,
            "TARGET_SHA": target,
            "BUDGET_RUB": budget,
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("mode", "inputs", "argv"),
    [
        ("continue", {}, ["continue", "--pr", "42"]),
        (
            "translate",
            {"source": "a" * 40, "budget": "1e-3"},
            ["translate", "--pr", "42", "--source-sha", "a" * 40, "--budget-rub", "1e-3"],
        ),
        (
            "verify",
            {"source": "a" * 40, "target": "b" * 40},
            ["verify", "--pr", "42", "--source-sha", "a" * 40, "--target-sha", "b" * 40],
        ),
    ],
)
def test_action_executes_exact_mode_arguments(mode, inputs, argv):
    result = run_action_inputs(mode, **inputs)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["-m", "ydbdoc_review_ng.cli", *argv]


@pytest.mark.parametrize(
    ("mode", "inputs"),
    [
        ("continue", {"source": "a" * 40}),
        ("continue", {"target": " "}),
        ("continue", {"budget": "0"}),
        ("continue", {"pr": "0"}),
        ("unknown", {}),
        ("translate", {"budget": "5"}),
        ("translate", {"source": "a" * 40}),
        ("translate", {"source": "a" * 40, "budget": "NaN"}),
        ("translate", {"source": "a" * 40, "budget": "Infinity"}),
        ("translate", {"source": "a" * 40, "budget": "-1"}),
        ("translate", {"source": "a" * 40, "budget": "5", "target": "b" * 40}),
        ("verify", {"source": "a" * 40}),
        ("verify", {"target": "b" * 40}),
        ("verify", {"source": "a" * 40, "target": "b" * 40, "budget": "0"}),
    ],
)
def test_action_rejects_invalid_or_cross_mode_inputs_before_python(mode, inputs):
    result = run_action_inputs(mode, **inputs)
    assert result.returncode == 2
    assert result.stdout == ""


def test_consumer_template_requires_trusted_pin_and_pr_label_boundary():
    template = (ROOT / "docs/examples/doc_continue.yml").read_text()
    assert not (ROOT / ".github/workflows/doc_continue.yml").exists()
    rendered = template.replace("REVIEWED_1_1_0_COMMIT_SHA", "a" * 40)
    workflow = yaml.safe_load(rendered)
    # PyYAML uses YAML 1.1, where an unquoted `on` is a boolean key.
    assert workflow.get("on", workflow.get(True)) == {"pull_request_target": {"types": ["labeled"]}}
    assert workflow["concurrency"] == {
        "group": "doc-continue-${{ github.repository }}-${{ github.event.pull_request.number }}",
        "cancel-in-progress": False,
    }
    job = workflow["jobs"]["doc_continue"]
    assert "github.event.label.name == 'doc_continue'" in job["if"]
    assert job["env"]["YDBDOC_ALLOWED_ACTORS"] == "${{ vars.YDBDOC_ALLOWED_ACTORS }}"
    assert job["env"]["YDB_GH_TOKEN"] == "${{ secrets.YDB_GH_TOKEN }}"
    assert job["env"]["YDB_SA_KEY"] == "${{ secrets.YDB_SA_KEY }}"
    assert job["env"]["YANDEX_API_KEY"] == "${{ secrets.YANDEX_API_KEY }}"
    steps = job["steps"]
    assert len(steps) == 1  # Authorization and failed-job audit run inside the trusted runtime.
    action = steps[0]
    assert re.fullmatch(
        r"ydb-platform/ydbdoc-review-ng/\.github/actions/doc-review@[0-9a-f]{40}", action["uses"]
    )
    assert action["with"] == {
        "mode": "continue",
        "pr": "${{ github.event.pull_request.number }}",
    }


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
