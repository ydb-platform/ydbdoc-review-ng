from decimal import Decimal
from pathlib import Path

import pytest

from ydbdoc_review_ng.application import (
    ContinueWorkflowInput,
    TranslateWorkflowInput,
    VerifyWorkflowInput,
    WorkflowError,
    WorkflowResult,
    WorkflowStage,
)
from ydbdoc_review_ng.cli import main
from ydbdoc_review_ng.domain import GitSha, Mode
from ydbdoc_review_ng.errors import SafeDiagnosticError
from ydbdoc_review_ng.persistence import DailyBudgetExceeded
from ydbdoc_review_ng.quality import Verdict

pytestmark = pytest.mark.unit
SHA = "a" * 40
TARGET = "b" * 40
ROOT = Path(__file__).resolve().parents[2]
PRIVATE_ARGUMENT = "PRIVATE_OPERATOR_CONTEXT_SENTINEL"
VALID_ARGUMENTS = {
    "translate": ["translate", "--pr", "42", "--source-sha", SHA, "--budget-rub", "5"],
    "verify": ["verify", "--pr", "42", "--source-sha", SHA, "--target-sha", TARGET],
    "continue": ["continue", "--pr", "42"],
}


class Dispatcher:
    def __init__(self):
        self.requests = []

    def doc_translate(self, request):
        self.requests.append(request)

    def doc_verify(self, request):
        self.requests.append(request)

    def doc_continue(self, request):
        self.requests.append(request)


@pytest.mark.parametrize(
    "args",
    [
        [PRIVATE_ARGUMENT],
        ["--unknown", PRIVATE_ARGUMENT],
        *[args + ["--unknown", PRIVATE_ARGUMENT] for args in VALID_ARGUMENTS.values()],
        *[[*args[:2], PRIVATE_ARGUMENT, *args[3:]] for args in VALID_ARGUMENTS.values()],
        *[[*args[:2], "0", *args[3:]] for args in VALID_ARGUMENTS.values()],
        *[
            ["continue", "--pr", "42", flag, PRIVATE_ARGUMENT]
            for flag in ("--source-sha", "--target-sha", "--budget-rub", "--context")
        ],
        ["translate", "--pr", "42", "--source-sha", PRIVATE_ARGUMENT, "--budget-rub", "5"],
        ["verify", "--pr", "42", "--source-sha", PRIVATE_ARGUMENT, "--target-sha", TARGET],
        ["verify", "--pr", "42", "--source-sha", SHA, "--target-sha", PRIVATE_ARGUMENT],
        ["translate", "--pr", "42", "--source-sha", SHA, "--budget-rub", PRIVATE_ARGUMENT],
        ["translate", "--pr", "42", "--source-sha", SHA, "--budget-rub", "NaN"],
    ],
)
def test_malformed_cli_inputs_never_echo_argument_values_or_construct_runtime(args, capsys):
    def forbidden():
        pytest.fail("malformed inputs must not construct the runtime")

    try:
        status = main(args, factory=forbidden)
    except SystemExit as error:
        status = error.code
    assert status == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert PRIVATE_ARGUMENT not in captured.err
    assert captured.err.endswith("Invalid workflow inputs\n")
    if captured.err != "Invalid workflow inputs\n":
        assert captured.err.startswith("usage: ydbdoc-review ")


@pytest.mark.parametrize("mode", VALID_ARGUMENTS)
def test_redacted_parser_keeps_factory_and_workflow_failure_boundaries(mode, capsys):
    def broken_factory():
        raise RuntimeError(PRIVATE_ARGUMENT)

    assert main(VALID_ARGUMENTS[mode], factory=broken_factory) == 2
    assert capsys.readouterr().err == "Workflow runtime factory is missing or unavailable\n"

    def broken_workflow(request):
        raise RuntimeError(PRIVATE_ARGUMENT)

    dispatcher = Dispatcher()
    setattr(dispatcher, "doc_" + mode, broken_workflow)
    assert main(VALID_ARGUMENTS[mode], dispatcher=dispatcher) == 1
    assert capsys.readouterr().err == "Workflow failed; inspect the job audit\n"


def test_t017_f02_red_doc_verify_result_returns_failing_cli_status() -> None:
    class RedDispatcher(Dispatcher):
        def doc_verify(self, request):
            super().doc_verify(request)
            return WorkflowResult("job-1", Mode.DOC_VERIFY, GitSha(TARGET), Verdict.RED, False)

    dispatcher = RedDispatcher()

    assert (
        main(
            ["verify", "--pr", "123", "--source-sha", SHA, "--target-sha", TARGET],
            dispatcher=dispatcher,
        )
        == 1
    )
    assert dispatcher.requests == [VerifyWorkflowInput(123, GitSha(SHA), GitSha(TARGET))]


def test_t017_f13_quota_message_is_exact_while_arbitrary_errors_stay_redacted(capsys) -> None:
    class QuotaDispatcher(Dispatcher):
        def doc_translate(self, request):
            super().doc_translate(request)
            raise DailyBudgetExceeded

    args = ["translate", "--pr", "42", "--source-sha", SHA, "--budget-rub", "5"]
    assert main(args, dispatcher=QuotaDispatcher()) == 1
    assert capsys.readouterr().err == "квота на сегодня исчерпана, попробуйте позже\n"

    class SecretDispatcher(Dispatcher):
        def doc_translate(self, request):
            super().doc_translate(request)
            raise RuntimeError("private-token")

    assert main(args, dispatcher=SecretDispatcher()) == 1
    error = capsys.readouterr().err
    assert error == "Workflow failed; inspect the job audit\n"
    assert "private-token" not in error


def test_sanitized_workflow_failure_exposes_only_mode_and_stage(capsys) -> None:
    class FailedDispatcher(Dispatcher):
        def doc_translate(self, request):
            super().doc_translate(request)
            raise WorkflowError(Mode.DOC_TRANSLATE, WorkflowStage.PREPARE)

    assert main(VALID_ARGUMENTS["translate"], dispatcher=FailedDispatcher()) == 1
    assert (
        capsys.readouterr().err
        == "doc_translate workflow failed during prepare; inspect the job audit\n"
    )


def test_sanitized_workflow_failure_exposes_fixed_boundary_diagnostic(capsys) -> None:
    class FailedDispatcher(Dispatcher):
        def doc_continue(self, request):
            super().doc_continue(request)
            raise WorkflowError(
                Mode.DOC_CONTINUE,
                WorkflowStage.AUTHORIZE,
                SafeDiagnosticError("continue_checkpoint_missing"),
            )

    assert main(["continue", "--pr", "50858"], dispatcher=FailedDispatcher()) == 1
    assert (
        capsys.readouterr().err
        == "doc_continue workflow failed during authorize: "
        "continue_checkpoint_missing; inspect the job audit\n"
    )


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        (
            "dependency_file_limit_exceeded",
            "Перевод остановлен: число файлов зависимостей превышает установленный лимит. "
            "Уменьшите scope перевода или увеличьте лимит.\n",
        ),
        (
            "source_character_limit_exceeded",
            "Перевод остановлен: объём исходного текста превышает установленный лимит. "
            "Разделите перевод на части или увеличьте лимит.\n",
        ),
    ],
)
def test_scope_limit_failures_are_explicitly_reported_to_the_user(
    diagnostic, expected, capsys
) -> None:
    class FailedDispatcher(Dispatcher):
        def doc_translate(self, request):
            super().doc_translate(request)
            raise WorkflowError(
                Mode.DOC_TRANSLATE,
                WorkflowStage.PREPARE,
                SafeDiagnosticError(diagnostic),
            )

    assert main(VALID_ARGUMENTS["translate"], dispatcher=FailedDispatcher()) == 1
    assert capsys.readouterr().err == expected


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_error"),
    [
        ("success", 0, ""),
        ("red", 1, ""),
        ("budget", 1, "квота на сегодня исчерпана, попробуйте позже\n"),
        ("failure", 1, "Workflow failed; inspect the job audit\n"),
    ],
)
def test_cli_shuts_down_runtime_after_every_workflow_outcome(
    outcome, expected_status, expected_error, capsys
):
    class ClosingDispatcher(Dispatcher):
        def __init__(self):
            super().__init__()
            self.shutdowns = 0

        def doc_translate(self, request):
            super().doc_translate(request)
            if outcome == "red":
                return WorkflowResult(
                    "job-1", Mode.DOC_TRANSLATE, GitSha(TARGET), Verdict.RED, False
                )
            if outcome == "budget":
                raise DailyBudgetExceeded
            if outcome == "failure":
                raise RuntimeError(PRIVATE_ARGUMENT)
            return None

        def shutdown(self):
            self.shutdowns += 1

    dispatcher = ClosingDispatcher()

    assert main(VALID_ARGUMENTS["translate"], dispatcher=dispatcher) == expected_status
    assert dispatcher.shutdowns == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected_error
    assert PRIVATE_ARGUMENT not in captured.err


def test_cli_hides_shutdown_failures_without_changing_success(capsys) -> None:
    shutdowns = []

    class BrokenShutdownDispatcher(Dispatcher):
        def shutdown(self):
            shutdowns.append("attempted")
            raise RuntimeError(PRIVATE_ARGUMENT)

    assert main(VALID_ARGUMENTS["translate"], dispatcher=BrokenShutdownDispatcher()) == 0
    assert shutdowns == ["attempted"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_dispatches_exact_workflow_inputs():
    dispatcher = Dispatcher()
    assert (
        main(
            ["translate", "--pr", "42", "--source-sha", SHA, "--budget-rub", "5.5"],
            dispatcher=dispatcher,
        )
        == 0
    )
    assert (
        main(
            ["verify", "--pr", "123", "--source-sha", SHA, "--target-sha", TARGET],
            dispatcher=dispatcher,
        )
        == 0
    )
    assert dispatcher.requests == [
        TranslateWorkflowInput(42, GitSha(SHA), Decimal("5.5")),
        VerifyWorkflowInput(123, GitSha(SHA), GitSha(TARGET)),
    ]


@pytest.mark.parametrize("mode", ["unknown"])
def test_unknown_modes_never_dispatch(mode):
    dispatcher = Dispatcher()
    with pytest.raises(SystemExit) as error:
        main([mode], dispatcher=dispatcher)
    assert error.value.code == 2
    assert dispatcher.requests == []


def test_continue_dispatches_only_pr_after_validation():
    dispatcher = Dispatcher()
    assert main(["continue", "--pr", "42"], factory=lambda: dispatcher) == 0
    assert dispatcher.requests == [ContinueWorkflowInput(42)]


@pytest.mark.parametrize("flag", ["--source-sha", "--target-sha", "--budget-rub"])
def test_continue_rejects_snapshot_and_budget_overrides_before_factory(flag):
    def forbidden():
        pytest.fail("invalid inputs must never construct the runtime")

    with pytest.raises(SystemExit) as error:
        main(["continue", "--pr", "42", flag, SHA], factory=forbidden)
    assert error.value.code == 2


@pytest.mark.parametrize("pr", ["0", "-1"])
def test_continue_rejects_invalid_pr_before_factory(pr):
    def forbidden():
        pytest.fail("invalid PR must never construct the runtime")

    assert main(["continue", "--pr", pr], factory=forbidden) == 2


@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("translate", ["--source-sha", SHA, "--budget-rub", "5"]),
        ("verify", ["--source-sha", SHA, "--target-sha", TARGET]),
        ("continue", []),
    ],
)
def test_every_red_workflow_result_is_a_failed_cli_run(mode, extra):
    dispatcher = Dispatcher()
    setattr(
        dispatcher,
        "doc_" + mode,
        lambda request: WorkflowResult(
            "job", Mode("doc_" + mode), GitSha(TARGET), Verdict.RED, False
        ),
    )
    assert main([mode, "--pr", "42", *extra], dispatcher=dispatcher) == 1


def test_missing_runtime_is_explicit_and_factory_errors_hide_secrets(monkeypatch, capsys):
    monkeypatch.delenv("YDBDOC_RUNTIME_FACTORY", raising=False)
    args = ["translate", "--pr", "42", "--source-sha", SHA, "--budget-rub", "5"]
    assert main(args) == 2
    assert "runtime" in capsys.readouterr().err

    def broken_factory():
        raise RuntimeError("private-token")

    assert main(args, factory=broken_factory) == 2
    assert "private-token" not in capsys.readouterr().err


def test_factory_is_injected_after_arguments_are_valid():
    dispatcher = Dispatcher()
    assert (
        main(
            ["verify", "--pr", "123", "--source-sha", SHA, "--target-sha", TARGET],
            factory=lambda: dispatcher,
        )
        == 0
    )
    assert dispatcher.requests == [VerifyWorkflowInput(123, GitSha(SHA), GitSha(TARGET))]


def test_two_workflow_entrypoints_share_one_composite_action():
    paths = sorted((ROOT / ".github/workflows").glob("doc_*.yml"))
    assert [p.stem for p in paths] == ["doc_translate", "doc_verify"]
    actions = list((ROOT / ".github/actions").glob("*/action.yml"))
    assert len(actions) == 1
    action = actions[0].read_text()
    assert "using: composite" in action
    assert '"$MODE"' in action
    assert "YDBDOC_RUNTIME_FACTORY" in action
    for path, mode in zip(paths, ["translate", "verify"], strict=True):
        workflow = path.read_text()
        assert "workflow_dispatch:" in workflow
        assert "uses: ./.github/actions/doc-review" in workflow
        assert f"mode: {mode}" in workflow
        assert "contents: write" in workflow
        assert "pull-requests: write" in workflow
        assert "secrets.GITHUB_TOKEN" in workflow
        assert "YDB_SA_KEY: ${{ secrets.YDB_SA_KEY }}" in workflow
        assert "doc_continue" not in workflow + action
