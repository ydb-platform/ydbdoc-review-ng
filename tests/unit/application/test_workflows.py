from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ydbdoc_review_ng.application.workflows import (
    AuthorizedRun,
    ImmutableRunSnapshot,
    LinearWorkflows,
    TranslateWorkflowInput,
    VerifyWorkflowInput,
    WorkflowCandidate,
    WorkflowError,
    WorkflowResult,
    WorkflowStage,
)
from ydbdoc_review_ng.domain import GitSha, Mode
from ydbdoc_review_ng.persistence import DailyBudgetExceeded, JobStatus
from ydbdoc_review_ng.quality import (
    CriticResult,
    QualityReviewResult,
    RepairErrorReason,
    Verdict,
)

SOURCE_SHA = GitSha("a" * 40)
TARGET_SHA = GitSha("b" * 40)
INITIAL_SHA = GitSha("c" * 40)
REPAIRED_SHA = GitSha("d" * 40)
NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)
SECRET = "confidential-source-and-model-payload"


@dataclass
class Scenario:
    fail_once_at: set[str] = field(default_factory=set)
    repair: bool = False
    invalid_repair: bool = False
    mixed_locale: bool = False
    events: list[str] = field(default_factory=list)
    model_calls: list[str] = field(default_factory=list)
    published_branches: list[str] = field(default_factory=list)

    def hit(self, event: str) -> None:
        self.events.append(event)
        if event in self.fail_once_at:
            self.fail_once_at.remove(event)
            if event == "budget":
                raise DailyBudgetExceeded
            raise RuntimeError(f"{SECRET}:{event}")


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FailingAfterStartClock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError(SECRET)
        return NOW


class FakePersistence:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.finished_errors: list[str | None] = []
        self.finished_target_shas: list[str | None] = []

    def start_job(
        self,
        mode: Mode,
        /,
        *,
        pr_number: int,
        source_sha: str,
        target_sha: str | None,
        started_at: datetime,
    ) -> str:
        assert mode in {Mode.DOC_TRANSLATE, Mode.DOC_VERIFY}
        assert pr_number == 42
        assert source_sha == SOURCE_SHA.value
        assert target_sha in {None, TARGET_SHA.value}
        assert started_at == NOW
        self.scenario.hit("job:start")
        return "job-42"

    def finish_job(
        self,
        job_id: str,
        status: JobStatus,
        /,
        *,
        error: str | None,
        finished_at: datetime,
        target_sha: str | None = None,
    ) -> None:
        assert job_id == "job-42"
        assert finished_at == NOW
        self.finished_errors.append(error)
        self.finished_target_shas.append(target_sha)
        self.scenario.hit(f"job:finish:{status.value}")

    def check_daily_budget(self, mode: Mode, /, *, limit_rub: Decimal, now: datetime) -> None:
        assert mode is Mode.DOC_TRANSLATE
        assert limit_rub == Decimal(100)
        assert now == NOW
        self.scenario.hit("budget")


class FakeSource:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario

    def authorize_translate(self, request: TranslateWorkflowInput, /) -> AuthorizedRun:
        assert request.pr_number == 42
        self.scenario.hit("authorize:translate")
        return AuthorizedRun(Mode.DOC_TRANSLATE, "translation/pr-42", None, object())

    def snapshot_translate(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot:
        assert authorization.mode is Mode.DOC_TRANSLATE
        self.scenario.hit("snapshot:translate")
        return ImmutableRunSnapshot(
            Mode.DOC_TRANSLATE,
            SOURCE_SHA,
            None,
            authorization.branch,
            object(),
        )

    def authorize_verify(self, request: VerifyWorkflowInput, /) -> AuthorizedRun:
        assert request.target_sha == TARGET_SHA
        self.scenario.hit("authorize:verify")
        return AuthorizedRun(Mode.DOC_VERIFY, "translation/pr-42", TARGET_SHA, object())

    def snapshot_verify(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot:
        assert authorization.mode is Mode.DOC_VERIFY
        self.scenario.hit("snapshot:verify")
        return ImmutableRunSnapshot(
            Mode.DOC_VERIFY,
            SOURCE_SHA,
            TARGET_SHA,
            authorization.branch,
            object(),
        )


class FakeContent:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario

    def prepare_translation(self, snapshot: ImmutableRunSnapshot, /) -> WorkflowCandidate:
        assert snapshot.mode is Mode.DOC_TRANSLATE
        self.scenario.hit("prepare:direction-scope-translate-assemble-reparse")
        if self.scenario.mixed_locale:
            self.scenario.model_calls.append("direction")
        self.scenario.model_calls.append("translate")
        return WorkflowCandidate(b"initial-candidate:" + SECRET.encode(), object())

    def load_verification_candidate(self, snapshot: ImmutableRunSnapshot, /) -> WorkflowCandidate:
        assert snapshot.mode is Mode.DOC_VERIFY
        self.scenario.hit("load:verify-candidate")
        return WorkflowCandidate(b"current-target:" + SECRET.encode(), object())

    def validate_candidate(
        self,
        snapshot: ImmutableRunSnapshot,
        candidate: WorkflowCandidate,
        /,
    ) -> None:
        del snapshot
        phase = "repair" if candidate.content == b"repaired-candidate" else "initial"
        self.scenario.hit(f"validate:{phase}")


class FakeReviewer:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario

    def review(
        self,
        snapshot: ImmutableRunSnapshot,
        candidate: WorkflowCandidate,
        /,
        *,
        before_final_critic: Callable[[bytes], None] | None = None,
    ) -> QualityReviewResult:
        del snapshot
        self.scenario.hit("review:t011")
        self.scenario.hit("review:primary-critic")
        self.scenario.model_calls.append("critic")
        green = CriticResult(Verdict.GREEN, ())
        if not self.scenario.repair and not self.scenario.invalid_repair:
            return QualityReviewResult(
                candidate.content,
                None,
                candidate.content,
                green,
                green,
                False,
                False,
                None,
            )
        self.scenario.hit("review:repair")
        self.scenario.model_calls.append("repair")
        if self.scenario.invalid_repair:
            self.scenario.hit("review:final-critic")
            self.scenario.model_calls.append("final_critic")
            return QualityReviewResult(
                candidate.content,
                None,
                candidate.content,
                CriticResult(Verdict.RED, ()),
                green,
                True,
                False,
                RepairErrorReason.INVALID_RESPONSE,
            )
        if before_final_critic is not None:
            before_final_critic(b"repaired-candidate")
        self.scenario.hit("review:final-critic")
        self.scenario.model_calls.append("final_critic")
        return QualityReviewResult(
            candidate.content,
            b"repaired-candidate",
            b"repaired-candidate",
            CriticResult(Verdict.RED, ()),
            green,
            True,
            True,
            None,
        )


class FakePublisher:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario

    def publish(
        self,
        snapshot: ImmutableRunSnapshot,
        candidate: WorkflowCandidate,
        /,
    ) -> GitSha:
        self.scenario.published_branches.append(snapshot.branch)
        repair = candidate.content == b"repaired-candidate"
        self.scenario.hit("publish:repair" if repair else "publish:initial")
        return REPAIRED_SHA if repair else INITIAL_SHA


class FakeReporter:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario

    def update_current_pr(
        self,
        *,
        mode: Mode,
        pr_number: int,
        branch: str,
        commit_sha: GitSha,
        review: QualityReviewResult,
    ) -> None:
        assert mode in {Mode.DOC_TRANSLATE, Mode.DOC_VERIFY}
        assert pr_number == 42
        assert branch == "translation/pr-42"
        assert commit_sha in {TARGET_SHA, INITIAL_SHA, REPAIRED_SHA}
        assert review.final.verdict is Verdict.GREEN
        self.scenario.hit("report:current-pr-verdict")


def build_workflows(
    scenario: Scenario, *, clock: FakeClock | FailingAfterStartClock | None = None
) -> tuple[LinearWorkflows, FakePersistence]:
    persistence = FakePersistence(scenario)
    return (
        LinearWorkflows(
            clock=clock or FakeClock(),
            persistence=persistence,
            source=FakeSource(scenario),
            content=FakeContent(scenario),
            reviewer=FakeReviewer(scenario),
            publisher=FakePublisher(scenario),
            reporter=FakeReporter(scenario),
        ),
        persistence,
    )


def translate_input() -> TranslateWorkflowInput:
    return TranslateWorkflowInput(42, SOURCE_SHA, Decimal(100))


def verify_input() -> VerifyWorkflowInput:
    return VerifyWorkflowInput(42, SOURCE_SHA, TARGET_SHA)


@pytest.mark.parametrize("mode", ["translate", "verify"])
def test_model_binding_receives_new_audit_id_before_authorization(mode: str) -> None:
    scenario = Scenario()
    persistence = FakePersistence(scenario)
    bound: list[str] = []

    def bind(job_id: str) -> None:
        assert scenario.events == ["job:start"]
        bound.append(job_id)

    workflows = LinearWorkflows(
        clock=FakeClock(),
        persistence=persistence,
        source=FakeSource(scenario),
        content=FakeContent(scenario),
        reviewer=FakeReviewer(scenario),
        publisher=FakePublisher(scenario),
        reporter=FakeReporter(scenario),
        bind_models=bind,
    )
    result = (
        workflows.doc_translate(translate_input())
        if mode == "translate"
        else workflows.doc_verify(verify_input())
    )
    assert bound == [result.job_id]


def test_binding_failure_terminalizes_started_audit_without_other_effects() -> None:
    scenario = Scenario()
    persistence = FakePersistence(scenario)

    def bind(job_id: str) -> None:
        raise RuntimeError(SECRET)

    workflows = LinearWorkflows(
        clock=FakeClock(),
        persistence=persistence,
        source=FakeSource(scenario),
        content=FakeContent(scenario),
        reviewer=FakeReviewer(scenario),
        publisher=FakePublisher(scenario),
        reporter=FakeReporter(scenario),
        bind_models=bind,
    )
    with pytest.raises(WorkflowError) as error:
        workflows.doc_translate(translate_input())
    assert SECRET not in str(error.value)
    assert scenario.events == ["job:start", "job:finish:failed"]


def test_translate_success_publishes_once_then_reviews_and_terminalizes() -> None:
    scenario = Scenario()
    workflows, persistence = build_workflows(scenario)

    result = workflows.doc_translate(translate_input())

    assert result == WorkflowResult("job-42", Mode.DOC_TRANSLATE, INITIAL_SHA, Verdict.GREEN, False)
    assert scenario.events == [
        "job:start",
        "authorize:translate",
        "snapshot:translate",
        "budget",
        "prepare:direction-scope-translate-assemble-reparse",
        "validate:initial",
        "publish:initial",
        "review:t011",
        "review:primary-critic",
        "report:current-pr-verdict",
        "job:finish:succeeded",
    ]
    assert scenario.model_calls == ["translate", "critic"]
    assert scenario.published_branches == ["translation/pr-42"]
    assert persistence.finished_errors == [None]
    assert SECRET not in repr(result)


def test_translate_applies_one_t011_repair_and_publishes_exactly_twice() -> None:
    scenario = Scenario(repair=True)
    workflows, _ = build_workflows(scenario)

    result = workflows.doc_translate(translate_input())

    assert result.final_commit_sha == REPAIRED_SHA
    assert result.repair_applied is True
    assert scenario.events == [
        "job:start",
        "authorize:translate",
        "snapshot:translate",
        "budget",
        "prepare:direction-scope-translate-assemble-reparse",
        "validate:initial",
        "publish:initial",
        "review:t011",
        "review:primary-critic",
        "review:repair",
        "validate:repair",
        "publish:repair",
        "review:final-critic",
        "report:current-pr-verdict",
        "job:finish:succeeded",
    ]
    assert scenario.model_calls == ["translate", "critic", "repair", "final_critic"]
    assert scenario.published_branches == ["translation/pr-42", "translation/pr-42"]


def test_verify_success_never_checks_budget_or_translates_or_publishes() -> None:
    scenario = Scenario()
    workflows, _ = build_workflows(scenario)

    result = workflows.doc_verify(verify_input())

    assert result == WorkflowResult("job-42", Mode.DOC_VERIFY, TARGET_SHA, Verdict.GREEN, False)
    assert scenario.events == [
        "job:start",
        "authorize:verify",
        "snapshot:verify",
        "load:verify-candidate",
        "validate:initial",
        "review:t011",
        "review:primary-critic",
        "report:current-pr-verdict",
        "job:finish:succeeded",
    ]
    assert scenario.model_calls == ["critic"]
    assert scenario.published_branches == []


def test_verify_repair_validates_and_commits_once_to_same_branch() -> None:
    scenario = Scenario(repair=True)
    workflows, _ = build_workflows(scenario)

    result = workflows.doc_verify(verify_input())

    assert result.final_commit_sha == REPAIRED_SHA
    assert scenario.events == [
        "job:start",
        "authorize:verify",
        "snapshot:verify",
        "load:verify-candidate",
        "validate:initial",
        "review:t011",
        "review:primary-critic",
        "review:repair",
        "validate:repair",
        "publish:repair",
        "review:final-critic",
        "report:current-pr-verdict",
        "job:finish:succeeded",
    ]
    assert scenario.model_calls == ["critic", "repair", "final_critic"]
    assert scenario.published_branches == ["translation/pr-42"]


def test_t017_f10_success_terminal_audit_receives_final_translate_and_repair_shas() -> None:
    translate = Scenario()
    translate_workflows, translate_persistence = build_workflows(translate)
    translate_workflows.doc_translate(translate_input())

    verify = Scenario(repair=True)
    verify_workflows, verify_persistence = build_workflows(verify)
    verify_workflows.doc_verify(verify_input())

    assert translate_persistence.finished_target_shas == [INITIAL_SHA.value]
    assert verify_persistence.finished_target_shas == [REPAIRED_SHA.value]


def test_t017_r08_failed_after_initial_publication_audits_published_sha() -> None:
    scenario = Scenario(fail_once_at={"review:t011"})
    workflows, persistence = build_workflows(scenario)

    with pytest.raises(WorkflowError):
        workflows.doc_translate(translate_input())

    assert persistence.finished_target_shas == [INITIAL_SHA.value]


def test_t017_r08_failed_after_repair_publication_audits_repaired_sha() -> None:
    scenario = Scenario(repair=True, fail_once_at={"review:final-critic"})
    workflows, persistence = build_workflows(scenario)

    with pytest.raises(WorkflowError):
        workflows.doc_verify(verify_input())

    assert persistence.finished_target_shas == [REPAIRED_SHA.value]


def test_t017_r08_failed_before_any_target_is_known_audits_null_sha() -> None:
    scenario = Scenario(fail_once_at={"authorize:translate"})
    workflows, persistence = build_workflows(scenario)

    with pytest.raises(WorkflowError):
        workflows.doc_translate(translate_input())

    assert persistence.finished_target_shas == [None]


@pytest.mark.parametrize(
    ("failed_event", "expected_before_finish"),
    [
        ("authorize:translate", ["job:start", "authorize:translate"]),
        (
            "snapshot:translate",
            ["job:start", "authorize:translate", "snapshot:translate"],
        ),
        (
            "validate:initial",
            [
                "job:start",
                "authorize:translate",
                "snapshot:translate",
                "budget",
                "prepare:direction-scope-translate-assemble-reparse",
                "validate:initial",
            ],
        ),
        (
            "publish:initial",
            [
                "job:start",
                "authorize:translate",
                "snapshot:translate",
                "budget",
                "prepare:direction-scope-translate-assemble-reparse",
                "validate:initial",
                "publish:initial",
            ],
        ),
        (
            "review:t011",
            [
                "job:start",
                "authorize:translate",
                "snapshot:translate",
                "budget",
                "prepare:direction-scope-translate-assemble-reparse",
                "validate:initial",
                "publish:initial",
                "review:t011",
            ],
        ),
        (
            "report:current-pr-verdict",
            [
                "job:start",
                "authorize:translate",
                "snapshot:translate",
                "budget",
                "prepare:direction-scope-translate-assemble-reparse",
                "validate:initial",
                "publish:initial",
                "review:t011",
                "review:primary-critic",
                "report:current-pr-verdict",
            ],
        ),
    ],
)
def test_translate_phase_failure_terminalizes_and_stops_later_effects(
    failed_event: str, expected_before_finish: list[str]
) -> None:
    scenario = Scenario(fail_once_at={failed_event})
    workflows, persistence = build_workflows(scenario)

    with pytest.raises(WorkflowError) as captured:
        workflows.doc_translate(translate_input())

    assert scenario.events == [*expected_before_finish, "job:finish:failed"]
    assert persistence.finished_errors[-1] is not None
    assert SECRET not in str(captured.value)
    assert SECRET not in repr(captured.value)
    assert SECRET not in persistence.finished_errors[-1]


def test_verify_validation_failure_has_no_model_publish_or_report_effects() -> None:
    scenario = Scenario(fail_once_at={"validate:initial"})
    workflows, _ = build_workflows(scenario)

    with pytest.raises(WorkflowError) as captured:
        workflows.doc_verify(verify_input())

    assert captured.value.stage is WorkflowStage.VALIDATE
    assert scenario.events == [
        "job:start",
        "authorize:verify",
        "snapshot:verify",
        "load:verify-candidate",
        "validate:initial",
        "job:finish:failed",
    ]
    assert scenario.model_calls == []
    assert scenario.published_branches == []


@pytest.mark.parametrize("failed_event", ["validate:repair", "publish:repair"])
def test_repair_callback_failure_prevents_final_critic_and_report(failed_event: str) -> None:
    scenario = Scenario(repair=True, fail_once_at={failed_event})
    workflows, persistence = build_workflows(scenario)

    with pytest.raises(WorkflowError) as captured:
        workflows.doc_verify(verify_input())

    expected_before_failure = [
        "job:start",
        "authorize:verify",
        "snapshot:verify",
        "load:verify-candidate",
        "validate:initial",
        "review:t011",
        "review:primary-critic",
        "review:repair",
        "validate:repair",
    ]
    if failed_event == "publish:repair":
        expected_before_failure.append("publish:repair")
    assert scenario.events == [*expected_before_failure, "job:finish:failed"]
    assert scenario.model_calls == ["critic", "repair"]
    assert "review:final-critic" not in scenario.events
    assert "report:current-pr-verdict" not in scenario.events
    assert persistence.finished_errors[-1] == f"{captured.value.stage.value}_failed"


def test_invalid_repair_final_critics_original_without_second_publication() -> None:
    scenario = Scenario(invalid_repair=True)
    workflows, _ = build_workflows(scenario)

    result = workflows.doc_translate(translate_input())

    assert result.final_commit_sha == INITIAL_SHA
    assert result.repair_applied is False
    assert scenario.events == [
        "job:start",
        "authorize:translate",
        "snapshot:translate",
        "budget",
        "prepare:direction-scope-translate-assemble-reparse",
        "validate:initial",
        "publish:initial",
        "review:t011",
        "review:primary-critic",
        "review:repair",
        "review:final-critic",
        "report:current-pr-verdict",
        "job:finish:succeeded",
    ]
    assert scenario.model_calls == ["translate", "critic", "repair", "final_critic"]
    assert scenario.published_branches == ["translation/pr-42"]


def test_mixed_locale_exhausted_budget_after_snapshot_has_exact_error_and_zero_model_calls() -> (
    None
):
    scenario = Scenario(fail_once_at={"budget"}, mixed_locale=True)
    workflows, persistence = build_workflows(scenario)

    with pytest.raises(DailyBudgetExceeded) as captured:
        workflows.doc_translate(translate_input())

    assert str(captured.value) == "квота на сегодня исчерпана, попробуйте позже"
    assert scenario.events == [
        "job:start",
        "authorize:translate",
        "snapshot:translate",
        "budget",
        "job:finish:failed",
    ]
    assert scenario.model_calls == []
    assert persistence.finished_errors == [DailyBudgetExceeded.user_message]


def test_success_terminal_finish_failure_attempts_failed_finish_and_stays_non_echoing() -> None:
    scenario = Scenario(fail_once_at={"job:finish:succeeded"})
    workflows, persistence = build_workflows(scenario)

    with pytest.raises(WorkflowError) as captured:
        workflows.doc_verify(verify_input())

    assert captured.value.stage is WorkflowStage.TERMINAL_AUDIT
    assert scenario.events[-2:] == ["job:finish:succeeded", "job:finish:failed"]
    assert persistence.finished_errors[-1] == "terminal_audit_failed"
    assert SECRET not in str(captured.value)


def test_failed_terminal_finish_does_not_echo_original_or_finish_payloads() -> None:
    scenario = Scenario(
        fail_once_at={"authorize:verify", "job:finish:failed"},
    )
    workflows, persistence = build_workflows(scenario)

    with pytest.raises(WorkflowError) as captured:
        workflows.doc_verify(verify_input())

    assert captured.value.stage is WorkflowStage.TERMINAL_AUDIT
    assert scenario.events == [
        "job:start",
        "authorize:verify",
        "job:finish:failed",
    ]
    assert persistence.finished_errors == ["authorize_failed"]
    assert SECRET not in str(captured.value)
    assert SECRET not in repr(captured.value)


def test_clock_failure_after_job_creation_still_attempts_failed_terminal_audit() -> None:
    scenario = Scenario()
    workflows, persistence = build_workflows(scenario, clock=FailingAfterStartClock())

    with pytest.raises(WorkflowError) as captured:
        workflows.doc_translate(translate_input())

    assert captured.value.stage is WorkflowStage.BUDGET
    assert scenario.events == [
        "job:start",
        "authorize:translate",
        "snapshot:translate",
        "job:finish:failed",
    ]
    assert persistence.finished_errors == ["budget_failed"]
    assert SECRET not in str(captured.value)
