from decimal import Decimal
from pathlib import Path

import pytest

from ydbdoc_review_ng.application import TranslateWorkflowInput, VerifyWorkflowInput, WorkflowResult
from ydbdoc_review_ng.cli import main
from ydbdoc_review_ng.domain import GitSha, Mode
from ydbdoc_review_ng.persistence import DailyBudgetExceeded
from ydbdoc_review_ng.quality import Verdict

pytestmark = pytest.mark.unit
SHA = "a" * 40
TARGET = "b" * 40
ROOT = Path(__file__).resolve().parents[2]


class Dispatcher:
    def __init__(self):
        self.requests = []

    def doc_translate(self, request):
        self.requests.append(request)

    def doc_verify(self, request):
        self.requests.append(request)


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


@pytest.mark.parametrize("mode", ["continue", "unknown"])
def test_removed_and_unknown_modes_never_dispatch(mode):
    dispatcher = Dispatcher()
    with pytest.raises(SystemExit) as error:
        main([mode], dispatcher=dispatcher)
    assert error.value.code == 2
    assert dispatcher.requests == []


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
        assert "doc_continue" not in workflow + action
