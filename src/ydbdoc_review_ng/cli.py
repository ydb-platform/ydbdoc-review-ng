from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from ydbdoc_review_ng.application import TranslateWorkflowInput, VerifyWorkflowInput


class Dispatcher(Protocol):
    def doc_translate(self, request: TranslateWorkflowInput, /) -> object: ...
    def doc_verify(self, request: VerifyWorkflowInput, /) -> object: ...


def _runtime_factory() -> Dispatcher:
    """Load trusted deployment composition; no built-in live transport exists."""
    module, name = os.environ["YDBDOC_RUNTIME_FACTORY"].split(":", 1)
    factory = cast(Callable[[], Dispatcher], getattr(importlib.import_module(module), name))
    return factory()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ydbdoc-review")
    subcommands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("translate", "verify"):
        command = subcommands.add_parser(mode)
        command.add_argument("--pr", type=int, required=True)
        command.add_argument("--source-sha", required=True)
        if mode == "translate":
            command.add_argument("--budget-rub", required=True)
        else:
            command.add_argument("--target-sha", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    dispatcher: Dispatcher | None = None,
    factory: Callable[[], Dispatcher] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    from ydbdoc_review_ng.application import (
        TranslateWorkflowInput,
        VerifyWorkflowInput,
        WorkflowResult,
    )
    from ydbdoc_review_ng.domain import GitSha
    from ydbdoc_review_ng.persistence import DailyBudgetExceeded
    from ydbdoc_review_ng.quality import Verdict

    try:
        request: TranslateWorkflowInput | VerifyWorkflowInput
        if args.mode == "translate":
            budget = Decimal(args.budget_rub)
            if not budget.is_finite():
                raise ValueError("invalid budget")
            request = TranslateWorkflowInput(args.pr, GitSha(args.source_sha), budget)
        else:
            request = VerifyWorkflowInput(args.pr, GitSha(args.source_sha), GitSha(args.target_sha))
    except Exception:  # noqa: BLE001 - input diagnostics must not echo raw values.
        print("Invalid workflow inputs", file=sys.stderr)
        return 2
    try:
        runtime = dispatcher if dispatcher is not None else (factory or _runtime_factory)()
    except Exception:  # noqa: BLE001 - trusted factories can raise secret-bearing errors.
        print("Workflow runtime factory is missing or unavailable", file=sys.stderr)
        return 2
    try:
        if isinstance(request, TranslateWorkflowInput):
            runtime.doc_translate(request)
        else:
            result = runtime.doc_verify(request)
            if type(result) is WorkflowResult and result.verdict is Verdict.RED:
                return 1
    except DailyBudgetExceeded:
        print(DailyBudgetExceeded.user_message, file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - workflow/transport diagnostics stay in audit.
        print("Workflow failed; inspect the job audit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
