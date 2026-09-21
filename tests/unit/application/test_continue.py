from dataclasses import fields
from datetime import UTC, datetime

import pytest

from ydbdoc_review_ng import application
from ydbdoc_review_ng.domain import Mode
from ydbdoc_review_ng.persistence import JobStatus


def continue_input():
    result = getattr(application, "ContinueWorkflowInput", None)
    assert result is not None, "public ContinueWorkflowInput is required"
    return result


def test_continue_input_exposes_only_positive_pr_number():
    request = continue_input()(42)
    assert [field.name for field in fields(request)] == ["pr_number"]
    assert request.pr_number == 42


@pytest.mark.parametrize("invalid", [0, -1, True, "42", None])
def test_continue_input_rejects_invalid_pr(invalid):
    with pytest.raises(ValueError):
        continue_input()(invalid)


def test_continue_audits_and_binds_new_job_before_admission_failure():
    events = []

    class Boundary:
        def now(self):
            return datetime(2026, 9, 22, tzinfo=UTC)

        def start_job(self, mode, **kwargs):
            events.append(("start", mode, kwargs["source_sha"], kwargs["target_sha"]))
            return "new-job"

        def bind(self, job_id):
            events.append(("bind", job_id))

        def authorize_continue(self, pr, checkpoints, *, now):
            events.append(("admit", pr))
            raise ValueError("private operator context")

        def finish_job(self, job_id, status, **kwargs):
            events.append(("finish", job_id, status, kwargs["error"]))

    boundary = Boundary()
    runtime = application.LinearWorkflows(
        clock=boundary,
        persistence=boundary,
        source=boundary,
        content=boundary,
        reviewer=boundary,
        publisher=boundary,
        reporter=boundary,
        bind_models=boundary.bind,
    )
    request = continue_input()(42)
    with pytest.raises(application.WorkflowError, match="authorize") as error:
        runtime.doc_continue(request)
    assert "private" not in str(error.value)
    assert events == [
        ("start", Mode.DOC_CONTINUE, None, None),
        ("bind", "new-job"),
        ("admit", 42),
        ("finish", "new-job", JobStatus.FAILED, "authorize_failed"),
    ]
