from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ydbdoc_review_ng.application import (
    AuthorizedRun,
    ImmutableRunSnapshot,
    LinearWorkflows,
    TranslateWorkflowInput,
    VerifyWorkflowInput,
    WorkflowCandidate,
    WorkflowError,
    WorkflowStage,
)
from ydbdoc_review_ng.direction import (
    Direction,
    DirectionModelDecision,
    DirectionModelRequest,
    DirectionModelResponse,
    DirectionPairVerdict,
    DirectionSelectionState,
    select_direction,
)
from ydbdoc_review_ng.domain import (
    GitSha,
    Locale,
    Mode,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import (
    ChangedFileKind,
    ChangedMarkdownFile,
    LocalePairInventory,
    LocaleRoots,
    PairFileState,
    PairKey,
    SnapshotLocaleFile,
)
from ydbdoc_review_ng.models import ModelCallResult, ModelRequest
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.persistence import DailyBudgetExceeded, JobStatus, YdbPersistence
from ydbdoc_review_ng.plan import SourcePlan, fields_of
from ydbdoc_review_ng.publication import (
    FileChange,
    GitPublicationAdapter,
    MetadataChange,
    PublicationContext,
    PublicationPlan,
    metadata_plan,
)
from ydbdoc_review_ng.quality import QualityReviewResult, Verdict, review_translation
from ydbdoc_review_ng.reporting import (
    CheckResult,
    Comment,
    QAReporter,
    ReportContext,
    merge_readiness,
)
from ydbdoc_review_ng.scope import FileOperation
from ydbdoc_review_ng.translation import (
    TranslationRequest,
    assemble_candidate,
    build_translation_request,
    parse_translation_response,
    verify_protected_fragments,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
REAL_FIXTURE = FIXTURES / "pairs/authentication.slice"
MULTILANGUAGE_FIXTURE = FIXTURES / "e2e/multilanguage.md"
PR_51079_SHA = "5aab6d0e65926540eff571d03a018a492face7e1"
REAL_FIXTURE_SHA256 = "d13d910ccaefec0eab57c927317caa5f863081455a6a2371192a57443548a80b"
NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)
BRANCH = "translation/pr-42"
MODEL = "offline-model"


@pytest.fixture(autouse=True)
def deny_python_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network is denied in offline E2E tests")

    monkeypatch.setattr(socket, "create_connection", denied)


def run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
    )
    return result.stdout.decode().strip()


class GitRepoBackend:
    def __init__(
        self,
        root: Path,
        initial_files: Mapping[str, bytes],
        events: list[str],
    ) -> None:
        self.work = root / "work"
        self.remote = root / "remote.git"
        self.events = events
        self.prs: dict[tuple[str, str, str], int] = {}
        self.comments: dict[int, Comment] = {}
        self.commit_count = 0
        subprocess.run(
            ("git", "init", "--bare", str(self.remote)),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ("git", "init", "-b", "main", str(self.work)),
            check=True,
            capture_output=True,
        )
        run_git(self.work, "config", "user.email", "offline@example.test")
        run_git(self.work, "config", "user.name", "Offline E2E")
        for name, content in initial_files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        run_git(self.work, "add", ".")
        run_git(self.work, "commit", "-m", "initial")
        run_git(self.work, "remote", "add", "origin", str(self.remote))
        run_git(self.work, "push", "-u", "origin", "main")
        run_git(self.work, "switch", "-c", BRANCH)
        run_git(self.work, "push", "-u", "origin", BRANCH)

    @property
    def head(self) -> GitSha:
        return GitSha(run_git(self.work, "rev-parse", BRANCH))

    def read(self, ref: GitSha, path: RepoPath) -> bytes | None:
        result = subprocess.run(
            ("git", "-C", str(self.work), "show", f"{ref.value}:{path.value}"),
            check=False,
            capture_output=True,
        )
        return result.stdout if result.returncode == 0 else None

    def remote_head(self, branch: str = BRANCH) -> GitSha:
        return GitSha(run_git(self.remote, "rev-parse", f"refs/heads/{branch}"))

    def commit(self, context: PublicationContext, plan: PublicationPlan, /) -> GitSha:
        assert run_git(self.work, "rev-parse", context.branch) == context.current_head.value
        run_git(self.work, "switch", context.branch)
        for change in plan.files:
            path = self.work / change.path.value
            if change.after is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(change.after)
        self._apply_metadata(plan.metadata)
        run_git(self.work, "add", "-A")
        run_git(self.work, "commit", "-m", f"offline publication {self.commit_count + 1}")
        self.commit_count += 1
        self.events.append("publish")
        return GitSha(run_git(self.work, "rev-parse", "HEAD"))

    def _apply_metadata(self, metadata: tuple[MetadataChange, ...]) -> None:
        toc_path = self.work / ".toc.txt"
        redirect_path = self.work / ".redirects.txt"
        toc = toc_path.read_text().splitlines() if toc_path.exists() else []
        redirects = redirect_path.read_text().splitlines() if redirect_path.exists() else []
        for change in metadata:
            if change.kind == "toc_add":
                toc.append(change.new_path.value)
            elif change.kind == "toc_replace":
                assert change.old_path is not None
                toc = [
                    change.new_path.value if item == change.old_path.value else item for item in toc
                ]
            else:
                assert change.old_path is not None
                redirects.append(f"{change.old_path.value} -> {change.new_path.value}")
        if toc:
            toc_path.write_text("\n".join(toc) + "\n")
        if redirects:
            redirect_path.write_text("\n".join(redirects) + "\n")

    def push(self, context: PublicationContext, sha: GitSha, /) -> None:
        run_git(self.work, "push", "origin", f"{sha.value}:refs/heads/{context.branch}")
        assert self.remote_head(context.branch) == sha

    def find_pr(self, repository: str, branch: str, base: str, /) -> int | None:
        return self.prs.get((repository, branch, base))

    def create_pr(self, context: PublicationContext, sha: GitSha, /) -> int:
        assert self.remote_head(context.branch) == sha
        self.events.append("create_pr")
        self.prs[(context.repository, context.branch, context.base)] = 42
        return 42

    def update_pr(self, pr_number: int, context: PublicationContext, sha: GitSha, /) -> None:
        assert pr_number == 42
        assert self.remote_head(context.branch) == sha
        self.events.append("update_pr")

    def list_comments(self, pr_number: int, /) -> tuple[Comment, ...]:
        assert pr_number == 42
        return tuple(self.comments.values())

    def create_comment(self, pr_number: int, body: str, /) -> None:
        assert pr_number == 42
        self.comments[1] = Comment(1, True, body)

    def update_comment(self, pr_number: int, comment_id: int, body: str, /) -> None:
        assert pr_number == 42
        self.comments[comment_id] = Comment(comment_id, True, body)


class FakeYdbExecutor:
    def __init__(self, known_cost: Decimal = Decimal(0)) -> None:
        self.known_cost = known_cost
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(
        self, statement: str, parameters: Mapping[str, object], /
    ) -> Sequence[Mapping[str, object]]:
        self.calls.append((statement, dict(parameters)))
        if "SELECT SUM(cost_rub)" in statement:
            return ({"total_cost_rub": self.known_cost},)
        return ()

    @property
    def terminal_rows(self) -> list[dict[str, object]]:
        return [
            parameters
            for _statement, parameters in self.calls
            if "finished_at" in parameters and "error" in parameters
        ]


class FixedClock:
    def now(self) -> datetime:
        return NOW


class DirectionModel:
    def __init__(self, calls: list[str], verdict: DirectionPairVerdict) -> None:
        self.calls = calls
        self.verdict = verdict

    def invoke(self, request: DirectionModelRequest, /) -> DirectionModelResponse:
        self.calls.append("direction")
        return DirectionModelResponse(
            tuple(DirectionModelDecision(pair.key, self.verdict) for pair in request.pairs)
        )


class TranslationModel:
    def __init__(self, calls: list[str], prefix: str, *, malformed: bool = False) -> None:
        self.calls = calls
        self.prefix = prefix
        self.malformed = malformed

    def invoke(self, request: TranslationRequest, /) -> str:
        self.calls.append("translate")
        if self.malformed:
            return "{malformed"
        return json.dumps(
            {field.field_id: self.prefix + field.text for field in request.fields},
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class ReviewContext:
    source: bytes
    source_plan: SourcePlan
    request: TranslationRequest
    target_path: RepoPath
    source_locale: Locale
    target_locale: Locale


@dataclass
class Case:
    backend: GitRepoBackend
    source: bytes
    source_path: RepoPath
    target_path: RepoPath
    source_locale: Locale
    target_locale: Locale
    calls: list[str]
    events: list[str]
    translation_prefix: str = "Translated: "
    malformed_translation: bool = False
    mixed_locale: bool = False
    repair: bool = False


def inventory(case: Case, snapshot: SnapshotRef) -> LocalePairInventory:
    roots = LocaleRoots(RepoPath("ydb/docs/ru"), RepoPath("ydb/docs/en"))
    key = PairKey(RepoPath("core/page.md"))
    ru_path = RepoPath("ydb/docs/ru/core/page.md")
    en_path = RepoPath("ydb/docs/en/core/page.md")
    changes = tuple(
        ChangedMarkdownFile(
            roots,
            ChangedFileKind.MODIFIED,
            locale,
            key,
            path,
            path,
            None,
            None,
        )
        for locale, path in ((Locale.RU, ru_path), (Locale.EN, en_path))
    )
    return LocalePairInventory(
        roots,
        key,
        SnapshotLocaleFile(roots, Locale.RU, key, ru_path, snapshot, case.source),
        SnapshotLocaleFile(roots, Locale.EN, key, en_path, snapshot, b"Incomplete target\n"),
        changes,
        PairFileState.BOTH_PRESENT,
    )


class SourceAdapter:
    def __init__(self, case: Case) -> None:
        self.case = case

    def _context(self, head: GitSha) -> PublicationContext:
        return PublicationContext("ydb-platform/ydb", BRANCH, "main", "main", head)

    def authorize_translate(self, request: TranslateWorkflowInput, /) -> AuthorizedRun:
        return AuthorizedRun(
            Mode.DOC_TRANSLATE, BRANCH, None, self._context(self.case.backend.head)
        )

    def snapshot_translate(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot:
        context = authorization.context
        assert type(context) is PublicationContext
        return ImmutableRunSnapshot(
            Mode.DOC_TRANSLATE,
            context.current_head,
            None,
            BRANCH,
            context,
        )

    def authorize_verify(self, request: VerifyWorkflowInput, /) -> AuthorizedRun:
        return AuthorizedRun(
            Mode.DOC_VERIFY,
            BRANCH,
            request.target_sha,
            self._context(request.target_sha),
        )

    def snapshot_verify(self, authorization: AuthorizedRun, /) -> ImmutableRunSnapshot:
        assert authorization.current_target_sha is not None
        return ImmutableRunSnapshot(
            Mode.DOC_VERIFY,
            authorization.current_target_sha,
            authorization.current_target_sha,
            BRANCH,
            authorization.context,
        )


class ContentAdapter:
    def __init__(self, case: Case, publisher: GitPublicationAdapter) -> None:
        self.case = case
        self.publisher = publisher

    def _context(self, snapshot: ImmutableRunSnapshot, target: bytes) -> ReviewContext:
        source_snapshot = SnapshotRef(RepositoryId("ydb-platform/ydb"), snapshot.source_sha)
        plan = build_markdown_plan(source_snapshot, self.case.source_path, self.case.source)
        request = build_translation_request(self.case.source, plan)
        target_plan = build_markdown_plan(source_snapshot, self.case.target_path, target)
        verify_protected_fragments(self.case.source, plan, target, target_plan)
        return ReviewContext(
            self.case.source,
            plan,
            request,
            self.case.target_path,
            self.case.source_locale,
            self.case.target_locale,
        )

    def prepare_translation(self, snapshot: ImmutableRunSnapshot, /) -> WorkflowCandidate:
        source_snapshot = SnapshotRef(RepositoryId("ydb-platform/ydb"), snapshot.source_sha)
        if self.case.mixed_locale:
            expected = (
                DirectionPairVerdict.RU_TO_EN
                if self.case.source_locale is Locale.RU
                else DirectionPairVerdict.EN_TO_RU
            )
            selected = select_direction(
                DirectionModel(self.case.calls, expected),
                (inventory(self.case, source_snapshot),),
            )
            assert selected.state is DirectionSelectionState.SELECTED
            assert selected.direction is (
                Direction.RU_TO_EN if self.case.source_locale is Locale.RU else Direction.EN_TO_RU
            )
        plan = build_markdown_plan(source_snapshot, self.case.source_path, self.case.source)
        request = build_translation_request(self.case.source, plan)
        raw = TranslationModel(
            self.case.calls,
            self.case.translation_prefix,
            malformed=self.case.malformed_translation,
        ).invoke(request)
        values = parse_translation_response(raw, request)
        candidate = assemble_candidate(self.case.source, plan, request, values)
        return WorkflowCandidate(candidate, self._context(snapshot, candidate))

    def load_verification_candidate(self, snapshot: ImmutableRunSnapshot, /) -> WorkflowCandidate:
        assert snapshot.target_sha is not None
        target = self.case.backend.read(snapshot.target_sha, self.case.target_path)
        assert target is not None
        return WorkflowCandidate(target, self._context(snapshot, target))

    def validate_candidate(
        self, snapshot: ImmutableRunSnapshot, candidate: WorkflowCandidate, /
    ) -> None:
        context = candidate.review_context
        assert type(context) is ReviewContext
        target_plan = build_markdown_plan(
            context.source_plan.source_snapshot,
            context.target_path,
            candidate.content,
        )
        verify_protected_fragments(
            context.source,
            context.source_plan,
            candidate.content,
            target_plan,
        )
        self.case.events.append("validate")
        self.publisher.validate_candidate(snapshot, candidate)


class CriticExecutor:
    def __init__(self, case: Case, context: ReviewContext, candidate: bytes) -> None:
        self.case = case
        self.context = context
        self.candidate = candidate

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        role = request.role.value
        self.case.calls.append(role)
        self.case.events.append(role)
        if role == "critic" and self.case.repair:
            target_plan = build_markdown_plan(
                self.context.source_plan.source_snapshot,
                self.context.target_path,
                self.candidate,
            )
            first_field = fields_of(target_plan)[0]
            snippet = self.candidate[first_field.span.start : first_field.span.end].decode()
            text = json.dumps(
                {
                    "verdict": "RED",
                    "findings": [
                        {
                            "repairable": True,
                            "reason": "The first field needs correction.",
                            "expected_correction": "Repair the first field.",
                            "searchable_snippet": snippet,
                            "target_path": self.context.target_path.value,
                            "target_line": first_field.lines.start,
                            "field_ids": [self.context.request.requested_ids[0]],
                        }
                    ],
                }
            )
        elif role == "repair":
            field = self.context.request.fields[0]
            text = json.dumps({field.field_id: "Repaired: " + field.text})
        else:
            text = json.dumps({"verdict": "GREEN", "findings": []})
        return ModelCallResult(text, None, ())


class ReviewAdapter:
    def __init__(self, case: Case) -> None:
        self.case = case

    def review(
        self,
        snapshot: ImmutableRunSnapshot,
        candidate: WorkflowCandidate,
        /,
        *,
        before_final_critic: Callable[[bytes], None] | None = None,
    ) -> QualityReviewResult:
        del snapshot
        context = candidate.review_context
        assert type(context) is ReviewContext
        return review_translation(
            CriticExecutor(self.case, context, candidate.content),
            model=MODEL,
            source=context.source,
            source_plan=context.source_plan,
            translation_request=context.request,
            target=candidate.content,
            target_path=context.target_path,
            source_locale=context.source_locale,
            target_locale=context.target_locale,
            before_final_critic=before_final_critic,
        )


def publication_plan(
    case: Case,
) -> Callable[[ImmutableRunSnapshot, WorkflowCandidate], PublicationPlan]:
    def build(snapshot: ImmutableRunSnapshot, candidate: WorkflowCandidate) -> PublicationPlan:
        context = snapshot.context
        assert type(context) is PublicationContext
        before = case.backend.read(context.current_head, case.target_path)
        return PublicationPlan((FileChange(case.target_path, before, candidate.content),), ())

    return build


def build_workflow(
    case: Case,
    ydb: FakeYdbExecutor,
) -> tuple[LinearWorkflows, GitPublicationAdapter]:
    publisher = GitPublicationAdapter(case.backend, publication_plan(case), lambda *_args: None)
    content = ContentAdapter(case, publisher)

    def report_context() -> ReportContext:
        head = (
            publisher.context.current_head if publisher.context is not None else case.backend.head
        )
        return ReportContext(case.backend.head, head, Decimal("0.25"))

    def checks() -> tuple[CheckResult, ...]:
        head = (
            publisher.context.current_head if publisher.context is not None else case.backend.head
        )
        return (
            CheckResult("doc_verify", head, "success"),
            CheckResult("build-docs", head, "success"),
        )

    source = SourceAdapter(case)
    verification_context = PublicationContext(
        "ydb-platform/ydb", BRANCH, "main", "main", case.backend.head
    )
    reporter = QAReporter(
        case.backend,
        publisher,
        report_context,
        checks,
        verification_context=verification_context,
    )
    return (
        LinearWorkflows(
            clock=FixedClock(),
            persistence=YdbPersistence(ydb),
            source=source,
            content=content,
            reviewer=ReviewAdapter(case),
            publisher=publisher,
            reporter=reporter,
        ),
        publisher,
    )


def make_case(
    tmp_path: Path,
    *,
    source: bytes,
    source_locale: Locale = Locale.RU,
    target: bytes = b"Existing target\n",
    **changes: object,
) -> Case:
    source_path = RepoPath(f"ydb/docs/{source_locale.value}/core/page.md")
    target_locale = Locale.EN if source_locale is Locale.RU else Locale.RU
    target_path = RepoPath(f"ydb/docs/{target_locale.value}/core/page.md")
    events: list[str] = []
    backend = GitRepoBackend(tmp_path, {target_path.value: target}, events)
    case = Case(
        backend,
        source,
        source_path,
        target_path,
        source_locale,
        target_locale,
        [],
        events,
    )
    for name, value in changes.items():
        setattr(case, name, value)
    return case


def translate_input(case: Case) -> TranslateWorkflowInput:
    return TranslateWorkflowInput(42, case.backend.head, Decimal(100))


def verify_input(case: Case) -> VerifyWorkflowInput:
    return VerifyWorkflowInput(42, case.backend.head, case.backend.head)


@pytest.mark.parametrize(
    ("source_locale", "prefix"),
    [(Locale.RU, "English: "), (Locale.EN, "Русский: ")],
)
def test_offline_translate_runs_real_pipeline_in_both_directions(
    tmp_path: Path,
    source_locale: Locale,
    prefix: str,
) -> None:
    real = REAL_FIXTURE.read_bytes()
    generated = MULTILANGUAGE_FIXTURE.read_bytes()
    source = real + b"\n" + generated if source_locale is Locale.RU else generated
    assert hashlib.sha256(real).hexdigest() == REAL_FIXTURE_SHA256
    provenance = (FIXTURES / "e2e/PROVENANCE.md").read_text()
    assert PR_51079_SHA in provenance and "[0, 694)" in provenance
    case = make_case(
        tmp_path,
        source=source,
        source_locale=source_locale,
        translation_prefix=prefix,
    )
    ydb = FakeYdbExecutor()
    workflows, _publisher = build_workflow(case, ydb)

    result = workflows.doc_translate(translate_input(case))

    target = case.backend.read(result.final_commit_sha, case.target_path)
    assert result.verdict is Verdict.GREEN
    assert target is not None and target != source
    assert case.backend.remote_head() == result.final_commit_sha
    assert run_git(case.backend.remote, "show", f"{BRANCH}:{case.target_path.value}").encode() in {
        target,
        target.rstrip(b"\n"),
    }
    for protected in (
        b"https://ydb.tech/docs/en/guide",
        b"/etc/ydb/config.yaml",
        b"`SELECT 1`",
        b'value := "unsupported fence stays protected"',
    ):
        assert protected in target
    for language in (b"cpp", b"java", b"javascript", b"python", b"bash", b"yaml", b"html"):
        assert b"```" + language in target
    assert case.calls == ["translate", "critic"]
    assert (
        case.events.index("publish") < case.events.index("critic") < case.events.index("create_pr")
    )
    assert ydb.terminal_rows[-1]["status"] == JobStatus.SUCCEEDED.value


def test_mixed_direction_byte_identical_result_creates_no_empty_pr(tmp_path: Path) -> None:
    source = REAL_FIXTURE.read_bytes()
    case = make_case(
        tmp_path,
        source=source,
        target=source,
        mixed_locale=True,
        translation_prefix="",
    )
    original_head = case.backend.head
    workflows, publisher = build_workflow(case, FakeYdbExecutor())

    result = workflows.doc_translate(translate_input(case))

    assert result.final_commit_sha == original_head
    assert publisher.noop
    assert case.backend.commit_count == 0
    assert case.backend.prs == {}
    assert case.backend.comments == {}
    assert case.calls == ["direction", "translate", "critic"]


def test_malformed_translation_blocks_publication_and_terminalizes_job(tmp_path: Path) -> None:
    case = make_case(
        tmp_path,
        source=MULTILANGUAGE_FIXTURE.read_bytes(),
        malformed_translation=True,
    )
    ydb = FakeYdbExecutor()
    workflows, _publisher = build_workflow(case, ydb)

    with pytest.raises(WorkflowError) as caught:
        workflows.doc_translate(translate_input(case))

    assert caught.value.stage is WorkflowStage.PREPARE
    assert case.backend.commit_count == 0
    assert case.backend.prs == {}
    assert case.calls == ["translate"]
    assert ydb.terminal_rows[-1]["status"] == JobStatus.FAILED.value
    assert ydb.terminal_rows[-1]["error"] == "prepare_failed"


@pytest.mark.parametrize("existing_pr", [False, True])
def test_repair_is_validated_and_published_before_final_critic(
    tmp_path: Path, existing_pr: bool
) -> None:
    case = make_case(
        tmp_path,
        source=b"# Read [guide](/docs/guide)\n",
        repair=True,
    )
    workflows, _publisher = build_workflow(case, FakeYdbExecutor())
    if existing_pr:
        case.backend.prs[("ydb-platform/ydb", BRANCH, "main")] = 42

    result = workflows.doc_translate(translate_input(case))

    assert result.repair_applied
    assert case.backend.commit_count == 2
    repair_index = case.events.index("repair")
    assert case.events[repair_index : repair_index + 4] == [
        "repair",
        "validate",
        "publish",
        "final_critic",
    ]
    pr_event = "update_pr" if existing_pr else "create_pr"
    assert [event for event in case.events if event.endswith("_pr")] == [pr_event]
    assert case.events.index("final_critic") < case.events.index(pr_event)
    repaired = case.backend.read(result.final_commit_sha, case.target_path)
    assert repaired is not None and repaired.startswith(b"# Repaired:")


def test_exhausted_budget_blocks_mixed_translate_before_every_model_call(
    tmp_path: Path,
) -> None:
    case = make_case(
        tmp_path,
        source=REAL_FIXTURE.read_bytes(),
        mixed_locale=True,
    )
    ydb = FakeYdbExecutor(Decimal(100))
    workflows, _publisher = build_workflow(case, ydb)

    with pytest.raises(DailyBudgetExceeded) as caught:
        workflows.doc_translate(translate_input(case))

    assert str(caught.value) == "квота на сегодня исчерпана, попробуйте позже"
    assert case.calls == []
    assert case.backend.commit_count == 0
    assert ydb.terminal_rows[-1]["error"] == DailyBudgetExceeded.user_message


def test_doc_verify_ignores_exhausted_budget(tmp_path: Path) -> None:
    source = b"Read [guide](/docs/guide) and `SELECT 1`\n"
    case = make_case(tmp_path, source=source, target=source)
    case.backend.prs[("ydb-platform/ydb", BRANCH, "main")] = 42
    ydb = FakeYdbExecutor(Decimal(1000))
    workflows, _publisher = build_workflow(case, ydb)

    result = workflows.doc_verify(verify_input(case))

    assert result.verdict is Verdict.GREEN
    assert case.calls == ["critic"]
    assert not any("SELECT SUM(cost_rub)" in statement for statement, _params in ydb.calls)
    assert ydb.terminal_rows[-1]["status"] == JobStatus.SUCCEEDED.value


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (b"See [guide](/good)\n", b"See [guide](/bad)\n"),
        (b"Edit /a/b/config.yaml\n", b"Edit /x/y/config.yaml\n"),
        (b"Run `SELECT 1`\n", b"Run `DROP TABLE x`\n"),
    ],
    ids=("url", "path", "code"),
)
def test_doc_verify_rejects_manual_protected_fragment_edits(
    tmp_path: Path,
    source: bytes,
    target: bytes,
) -> None:
    case = make_case(tmp_path, source=source, target=target)
    case.backend.prs[("ydb-platform/ydb", BRANCH, "main")] = 42
    ydb = FakeYdbExecutor()
    workflows, _publisher = build_workflow(case, ydb)

    with pytest.raises(WorkflowError) as caught:
        workflows.doc_verify(verify_input(case))

    assert caught.value.stage is WorkflowStage.LOAD_CANDIDATE
    assert case.calls == []
    assert case.backend.commit_count == 0
    assert ydb.terminal_rows[-1]["status"] == JobStatus.FAILED.value


@pytest.mark.parametrize(
    ("name", "operation", "is_new", "reachable"),
    [
        ("ordinary", FileOperation.TRANSLATE, False, False),
        ("reachable_add", FileOperation.TRANSLATE, True, True),
        ("unreachable_add", FileOperation.TRANSLATE, True, False),
        ("rename", FileOperation.RENAME_TARGET, False, False),
    ],
)
def test_publication_metadata_policy_materializes_in_real_git(
    tmp_path: Path,
    name: str,
    operation: FileOperation,
    is_new: bool,
    reachable: bool,
) -> None:
    old = RepoPath("ydb/docs/en/core/old.md")
    target = RepoPath(
        "ydb/docs/en/core/new.md"
        if name in {"reachable_add", "unreachable_add", "rename"}
        else "ydb/docs/en/core/old.md"
    )
    initial = {".toc.txt": old.value.encode() + b"\n"}
    if not is_new:
        initial[old.value] = b"old\n"
    events: list[str] = []
    backend = GitRepoBackend(tmp_path, initial, events)
    context = PublicationContext("ydb-platform/ydb", BRANCH, "main", "main", backend.head)
    snapshot = ImmutableRunSnapshot(Mode.DOC_TRANSLATE, backend.head, None, BRANCH, context)
    candidate = WorkflowCandidate(b"new\n", object())
    old_target = old if operation is FileOperation.RENAME_TARGET else None
    metadata = metadata_plan(
        operation,
        target,
        source_is_new=is_new,
        source_toc_reachable=reachable,
        old_target=old_target,
    )
    files = (
        (FileChange(old, b"old\n", None), FileChange(target, None, b"new\n"))
        if operation is FileOperation.RENAME_TARGET
        else (FileChange(target, None if is_new else b"old\n", b"new\n"),)
    )
    adapter = GitPublicationAdapter(
        backend,
        lambda _snapshot, _candidate: PublicationPlan(files, metadata),
        lambda *_args: None,
    )

    adapter.validate_candidate(snapshot, candidate)
    commit_sha = adapter.publish(snapshot, candidate)

    assert backend.remote_head() == commit_sha
    assert backend.read(commit_sha, target) == b"new\n"
    toc = backend.read(commit_sha, RepoPath(".toc.txt"))
    redirects = backend.read(commit_sha, RepoPath(".redirects.txt"))
    if name == "reachable_add":
        assert toc is not None and target.value.encode() in toc
        assert redirects is None
    elif name == "rename":
        assert toc is not None and old.value.encode() not in toc and target.value.encode() in toc
        assert redirects == f"{old.value} -> {target.value}\n".encode()
        assert backend.read(commit_sha, old) is None
    else:
        assert toc == old.value.encode() + b"\n"
        assert redirects is None


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        (("success", "success"), "GREEN"),
        (("pending", "success"), "YELLOW"),
        (("success", "stale"), "YELLOW"),
        (("failure", "success"), "RED"),
    ],
)
def test_same_head_readiness_requires_both_current_successes(
    checks: tuple[str, str], expected: str
) -> None:
    head = GitSha("a" * 40)
    stale = GitSha("b" * 40)
    doc_status, build_status = checks
    results = (
        CheckResult("doc_verify", head, doc_status),
        CheckResult(
            "build-docs",
            stale if build_status == "stale" else head,
            "success" if build_status == "stale" else build_status,
        ),
    )

    assert merge_readiness(head, results).status == expected
