from dataclasses import replace
from decimal import Decimal

import pytest

from ydbdoc_review_ng import reporting
from ydbdoc_review_ng.application import ImmutableRunSnapshot, WorkflowCandidate
from ydbdoc_review_ng.domain import GitSha, Mode, RepoPath
from ydbdoc_review_ng.publication import (
    FileChange,
    GitPublicationAdapter,
    MetadataChange,
    PublicationContext,
    PublicationError,
    PublicationPlan,
    metadata_plan,
)
from ydbdoc_review_ng.quality import CriticResult, Finding, QualityReviewResult, Verdict
from ydbdoc_review_ng.reporting import (
    CheckResult,
    Comment,
    QAReporter,
    ReportContext,
    merge_readiness,
    render_report,
)
from ydbdoc_review_ng.runtime_github import GitHubBackend
from ydbdoc_review_ng.scope import FileOperation

pytestmark = pytest.mark.unit
SOURCE = GitSha("a" * 40)
TARGET = GitSha("b" * 40)
COMMIT = GitSha("c" * 40)
PATH = RepoPath("ydb/docs/en/core/page.md")
OLD = RepoPath("ydb/docs/en/core/old.md")
SECRET = "private-request-response-token"


class Backend:
    def __init__(self):
        self.events = []
        self.prs = {}
        self.comments = {}
        self.fail = False

    def commit(self, context, plan):
        self.events.append(("commit", context, plan))
        if self.fail:
            raise RuntimeError(SECRET)
        return COMMIT

    def push(self, context, sha):
        self.events.append(("push", context, sha))

    def find_pr(self, repository, branch, base):
        return self.prs.get((repository, branch, base))

    def create_pr(self, context, sha):
        self.events.append(("create_pr", sha))
        self.prs[(context.repository, context.branch, context.base)] = 123
        return 123

    def update_pr(self, pr_number, context, sha):
        self.events.append(("update_pr", pr_number, sha))

    def list_comments(self, pr_number):
        return tuple(self.comments.values())

    def create_comment(self, pr_number, body):
        self.events.append(("create_comment", pr_number))
        self.comments[7] = Comment(7, True, body)

    def update_comment(self, pr_number, comment_id, body):
        self.events.append(("update_comment", pr_number, comment_id))
        self.comments[comment_id] = Comment(comment_id, True, body)


def setup_publication(*, before=b"Old", after=b"New", metadata=()):
    backend = Backend()
    context = PublicationContext("ydb-platform/ydb", "translation/42", "main", "main", TARGET)
    snapshot = ImmutableRunSnapshot(Mode.DOC_TRANSLATE, SOURCE, TARGET, context.branch, context)
    candidate = WorkflowCandidate(after, SECRET)
    plan = PublicationPlan((FileChange(PATH, before, after),), metadata)
    validated = []

    def validate(snapshot, candidate, frozen_plan):
        validated.append((snapshot, candidate, frozen_plan))

    adapter = GitPublicationAdapter(backend, lambda s, c: plan, validate)
    return backend, snapshot, candidate, adapter, validated


def review(verdict=Verdict.GREEN, findings=()):
    critic = CriticResult(verdict, findings)
    return QualityReviewResult(
        SECRET.encode(), None, SECRET.encode(), critic, critic, False, False, None
    )


def reporter(backend, publisher):
    return QAReporter(
        backend,
        publisher,
        lambda: ReportContext(SOURCE, TARGET, Decimal("1.25")),
        lambda: (
            CheckResult("doc_verify", COMMIT, "success"),
            CheckResult("build-docs", COMMIT, "success"),
        ),
    )


def test_publish_only_validated_plan_then_update_same_branch_pr():
    backend, snapshot, candidate, adapter, validated = setup_publication()
    with pytest.raises(PublicationError, match="unvalidated"):
        adapter.publish(snapshot, candidate)
    assert backend.events == []
    adapter.validate_candidate(snapshot, candidate)
    assert adapter.publish(snapshot, candidate) == COMMIT
    assert len(validated) == 1
    assert [e[0] for e in backend.events] == ["commit", "push"]
    assert backend.prs == {}
    reporter(backend, adapter).update_current_pr(
        mode=Mode.DOC_TRANSLATE,
        pr_number=42,
        branch=snapshot.branch,
        commit_sha=COMMIT,
        review=review(),
    )
    assert [e[0] for e in backend.events] == ["commit", "push", "create_pr", "create_comment"]
    assert backend.events[0][2].files == (FileChange(PATH, b"Old", b"New"),)
    adapter.validate_candidate(snapshot, candidate)
    adapter.publish(snapshot, candidate)
    assert [e[0] for e in backend.events][-2:] == ["commit", "push"]
    reporter(backend, adapter).update_current_pr(
        mode=Mode.DOC_TRANSLATE,
        pr_number=42,
        branch=snapshot.branch,
        commit_sha=COMMIT,
        review=review(),
    )
    assert [e[0] for e in backend.events][-2:] == ["update_pr", "update_comment"]
    assert len(backend.prs) == 1


def test_validation_receipt_cannot_publish_other_candidate_or_snapshot():
    backend, snapshot, candidate, adapter, _ = setup_publication()
    adapter.validate_candidate(snapshot, candidate)
    for snap, value in [
        (snapshot, replace(candidate, content=b"Unvalidated")),
        (replace(snapshot, source_sha=COMMIT), candidate),
    ]:
        with pytest.raises(PublicationError, match="unvalidated"):
            adapter.publish(snap, value)
    assert backend.events == []


@pytest.mark.parametrize("field,value", [("repository", "fork/ydb"), ("base", "stable")])
def test_repository_and_source_base_must_match_before_mutation(field, value):
    backend, snapshot, candidate, adapter, _ = setup_publication()
    snapshot = replace(snapshot, context=replace(snapshot.context, **{field: value}))
    with pytest.raises(PublicationError):
        adapter.validate_candidate(snapshot, candidate)
    assert backend.events == []


def test_byte_identical_and_empty_plan_are_zero_mutation_including_comment():
    for empty in (False, True):
        backend, snapshot, candidate, adapter, _ = setup_publication(before=b"New")
        if empty:
            adapter = GitPublicationAdapter(
                backend, lambda s, c: PublicationPlan((), ()), lambda *a: None
            )
        adapter.validate_candidate(snapshot, candidate)
        assert adapter.publish(snapshot, candidate) == TARGET
        reporter(backend, adapter).update_current_pr(
            mode=Mode.DOC_TRANSLATE,
            pr_number=42,
            branch=snapshot.branch,
            commit_sha=TARGET,
            review=review(),
        )
        assert backend.events == []
        assert backend.prs == {}


def test_backend_errors_do_not_echo_payload():
    backend, snapshot, candidate, adapter, _ = setup_publication()
    adapter.validate_candidate(snapshot, candidate)
    backend.fail = True
    with pytest.raises(PublicationError) as error:
        adapter.publish(snapshot, candidate)
    assert SECRET not in str(error.value)
    assert [e[0] for e in backend.events] == ["commit"]


def test_failed_validation_clears_previous_receipt_and_has_no_mutations():
    backend, snapshot, candidate, _, _ = setup_publication()
    calls = []

    def validator(snapshot, candidate, plan):
        calls.append(candidate.content)
        if candidate.content == b"invalid":
            raise ValueError(SECRET)

    adapter = GitPublicationAdapter(
        backend, lambda s, c: PublicationPlan((FileChange(PATH, b"Old", c.content),), ()), validator
    )
    adapter.validate_candidate(snapshot, candidate)
    with pytest.raises(PublicationError, match="validation_failed") as error:
        adapter.validate_candidate(snapshot, replace(candidate, content=b"invalid"))
    assert SECRET not in str(error.value)
    with pytest.raises(PublicationError, match="unvalidated"):
        adapter.publish(snapshot, candidate)
    assert calls == [b"New", b"invalid"]
    assert backend.events == []


def test_repair_commit_uses_published_head_and_validates_new_plan():
    backend, snapshot, candidate, _, _ = setup_publication()
    adapter = GitPublicationAdapter(
        backend,
        lambda s, c: PublicationPlan((FileChange(PATH, b"Old", c.content),), ()),
        lambda *a: None,
    )
    adapter.validate_candidate(snapshot, candidate)
    adapter.publish(snapshot, candidate)
    repaired = replace(candidate, content=b"Repaired")
    adapter.validate_candidate(snapshot, repaired)
    adapter.publish(snapshot, repaired)
    commits = [e for e in backend.events if e[0] == "commit"]
    assert commits[0][1].current_head == TARGET
    assert commits[1][1].current_head == COMMIT
    assert commits[1][2].files[0].after == b"Repaired"


def test_plan_does_not_create_chain_through_existing_redirect():
    with pytest.raises(PublicationError, match="redirect_chain"):
        PublicationPlan(
            (),
            (MetadataChange("redirect", OLD, PATH),),
            existing_redirects=(MetadataChange("redirect", RepoPath("older.md"), OLD),),
        )


def test_plans_reject_mutable_file_bytes_before_validation():
    with pytest.raises(PublicationError):
        FileChange(PATH, b"Old", bytearray(b"New"))


def test_verify_reports_on_existing_pr_without_requiring_publication():
    backend, snapshot, _candidate, publisher, _ = setup_publication()
    context = snapshot.context
    backend.prs[(context.repository, context.branch, context.base)] = 123
    qa = QAReporter(
        backend,
        publisher,
        lambda: ReportContext(SOURCE, TARGET, None),
        lambda: (),
        verification_context=context,
    )
    qa.update_current_pr(
        mode=Mode.DOC_VERIFY,
        pr_number=123,
        branch=snapshot.branch,
        commit_sha=TARGET,
        review=review(),
    )
    assert backend.events == [("create_comment", 123)]
    assert backend.comments[7].body.startswith("🟡 YELLOW")
    assert "неизвестна" in backend.comments[7].body


def test_report_cannot_label_an_old_commit_as_current_head():
    backend, snapshot, candidate, adapter, _ = setup_publication()
    adapter.validate_candidate(snapshot, candidate)
    adapter.publish(snapshot, candidate)
    with pytest.raises(PublicationError, match="report_context_mismatch"):
        reporter(backend, adapter).update_current_pr(
            mode=Mode.DOC_TRANSLATE,
            pr_number=42,
            branch=snapshot.branch,
            commit_sha=TARGET,
            review=review(),
        )
    assert backend.comments == {}


@pytest.mark.parametrize(
    "operation,is_new,reachable,old,expected",
    [
        (FileOperation.TRANSLATE, False, True, None, ()),
        (FileOperation.TRANSLATE, True, True, None, (MetadataChange("toc_add", None, PATH),)),
        (FileOperation.TRANSLATE, True, False, None, ()),
        (
            FileOperation.RENAME_TARGET,
            False,
            True,
            OLD,
            (MetadataChange("toc_replace", OLD, PATH), MetadataChange("redirect", OLD, PATH)),
        ),
        (
            FileOperation.RENAME_TARGET_AND_TRANSLATE,
            False,
            False,
            OLD,
            (MetadataChange("toc_replace", OLD, PATH), MetadataChange("redirect", OLD, PATH)),
        ),
    ],
)
def test_exact_metadata_matrix(operation, is_new, reachable, old, expected):
    assert (
        metadata_plan(
            operation, PATH, source_is_new=is_new, source_toc_reachable=reachable, old_target=old
        )
        == expected
    )


def test_publication_consumes_metadata_and_rejects_redirect_chains():
    actions = metadata_plan(FileOperation.RENAME_TARGET, PATH, old_target=OLD)
    backend, snapshot, candidate, adapter, _ = setup_publication(metadata=actions)
    adapter.validate_candidate(snapshot, candidate)
    adapter.publish(snapshot, candidate)
    assert backend.events[0][2].metadata == actions
    with pytest.raises(PublicationError, match="redirect_chain"):
        PublicationPlan(
            (),
            (
                MetadataChange("redirect", OLD, PATH),
                MetadataChange("redirect", PATH, RepoPath("next.md")),
            ),
        )


def test_one_bot_comment_created_then_updated_without_copying_transcripts():
    backend, snapshot, candidate, adapter, _ = setup_publication()
    adapter.validate_candidate(snapshot, candidate)
    adapter.publish(snapshot, candidate)
    qa = reporter(backend, adapter)
    backend.comments[99] = Comment(99, False, "<!-- ydbdoc-current-qa --> user quote")
    for verdict in (Verdict.RED, Verdict.GREEN):
        qa.update_current_pr(
            mode=Mode.DOC_TRANSLATE,
            pr_number=42,
            branch=snapshot.branch,
            commit_sha=COMMIT,
            review=review(verdict),
        )
    assert [e for e in backend.events if e[0].endswith("comment")] == [
        ("create_comment", 123),
        ("update_comment", 123, 7),
    ]
    assert backend.comments[7].body.startswith("🟢 GREEN")
    assert SECRET not in backend.comments[7].body
    assert backend.comments[99].body.endswith("user quote")


def test_t017_f11_pat_authored_verify_reports_update_one_marker_comment() -> None:
    _, snapshot, candidate, publisher, _ = setup_publication()
    publisher.validate_candidate(snapshot, candidate)
    publisher.publish(snapshot, candidate)
    comments: list[dict[str, object]] = []
    mutations: list[str] = []

    def transport(method: str, path: str, payload: object) -> object:
        if method == "GET" and path == "/user":
            return {"id": 42, "type": "User", "login": "pat-publisher"}
        if method == "GET" and path.endswith("/issues/123/comments?per_page=100"):
            return list(comments)
        if method == "POST" and path.endswith("/issues/123/comments"):
            assert isinstance(payload, dict)
            mutations.append("POST")
            comments.append(
                {
                    "id": 7,
                    "user": {"id": 42, "type": "User", "login": "pat-publisher"},
                    "body": payload["body"],
                }
            )
            return comments[-1]
        if method == "PATCH" and path.endswith("/issues/comments/7"):
            assert isinstance(payload, dict)
            mutations.append("PATCH")
            comments[0]["body"] = payload["body"]
            return comments[0]
        raise AssertionError((method, path, payload))

    qa = QAReporter(
        GitHubBackend(transport),
        publisher,
        lambda: ReportContext(SOURCE, TARGET, Decimal("1.25")),
        lambda: (
            CheckResult("doc_verify", COMMIT, "success"),
            CheckResult("build-docs", COMMIT, "success"),
        ),
    )
    for verdict in (Verdict.RED, Verdict.GREEN):
        qa.update_current_pr(
            mode=Mode.DOC_VERIFY,
            pr_number=123,
            branch=snapshot.branch,
            commit_sha=COMMIT,
            review=review(verdict),
        )

    assert mutations == ["POST", "PATCH"]
    assert len(comments) == 1
    assert comments[0]["body"].startswith("🟢 GREEN")


def test_red_report_is_short_russian_and_actionable_without_internal_details():
    finding = Finding(
        True, "Meaning reversed", "Preserve negation", "does not delete", PATH.value, 19
    )
    report = render_report(
        review(Verdict.RED, (finding,)), COMMIT, ReportContext(SOURCE, TARGET, Decimal("1.25")), ()
    )
    for value in [
        "🔴 RED",
        PATH.value,
        "строка 19",
        "does not delete",
        "Meaning reversed",
        "Preserve negation",
        "1.25 RUB",
        "/ydbdoc continue",
        "doc_continue",
    ]:
        assert value in report
    for internal in [
        "CI:",
        "Source SHA",
        "Target SHA",
        "Commit SHA",
        SOURCE.value,
        TARGET.value,
        COMMIT.value,
        "Cumulative",
    ]:
        assert internal not in report
    assert report.startswith("🔴 RED")
    assert SECRET not in report


def test_red_report_summarizes_each_file_instead_of_dumping_findings() -> None:
    other = "ydb/docs/en/core/other.md"
    findings = tuple(
        Finding(
            True,
            f"problem {index}",
            f"fix {index}",
            f"snippet {index}",
            other if index % 2 else PATH.value,
            index + 1,
        )
        for index in range(25)
    )

    report = render_report(
        review(Verdict.RED, findings),
        COMMIT,
        ReportContext(SOURCE, TARGET, Decimal("0.40")),
        (),
    )

    assert report.count("- строка ") == 2
    assert "ещё 12 замечаний в этом файле" in report
    assert "problem 24" not in report
    assert PATH.value in report
    assert other in report
    assert len(report) < 3_000


def test_report_never_renders_unknown_cost_as_zero() -> None:
    report = render_report(
        review(),
        COMMIT,
        ReportContext(SOURCE, TARGET, None),
        (),
    )

    assert "Стоимость запуска: неизвестна" in report
    assert "0 RUB" not in report


def test_probable_duplicate_keeps_green_checks_yellow_and_names_both_files() -> None:
    new_path = RepoPath("ydb/docs/en/core/dev/optimization/hints.md")
    old_path = RepoPath(
        "ydb/docs/en/core/dev/query-execution-optimization/query-hints.md"
    )
    context = ReportContext(
        SOURCE,
        TARGET,
        Decimal("2.50"),
        (reporting.ProbableDuplicate(new_path, old_path),),
    )

    report = render_report(
        review(),
        COMMIT,
        context,
        (
            CheckResult("doc_verify", COMMIT, "success"),
            CheckResult("build-docs", COMMIT, "success"),
        ),
    )

    assert report.startswith("🟡 YELLOW")
    assert new_path.value in report
    assert old_path.value in report
    assert "возможный дубликат" in report.lower()
    assert "разберитесь вручную" in report.lower()
    assert "doc_verify" in report


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_path", ""),
        ("target_path", " \t\n"),
        ("target_path", None),
        ("searchable_snippet", ""),
        ("searchable_snippet", " \t\n"),
        ("searchable_snippet", None),
        ("reason", ""),
        ("reason", " \t\n"),
        ("reason", None),
        ("expected_correction", ""),
        ("expected_correction", " \t\n"),
        ("expected_correction", None),
        ("target_line", 0),
        ("target_line", -1),
        ("target_line", True),
        ("target_line", 1.5),
        ("target_line", "1"),
    ],
)
def test_renderer_rejects_every_incomplete_or_mistyped_finding(field, value):
    complete = Finding(True, SECRET, "Preserve negation", "does not delete", PATH.value, 19)
    incomplete = replace(complete, **{field: value})
    with pytest.raises(PublicationError, match="invalid_finding") as error:
        render_report(
            review(Verdict.RED, (complete, incomplete)),
            COMMIT,
            ReportContext(SOURCE, TARGET, Decimal("1.25")),
            (),
        )
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("existing_comment", [False, True])
def test_incomplete_finding_cannot_create_or_update_qa_comment(existing_comment):
    backend, snapshot, _candidate, publisher, _ = setup_publication()
    context = snapshot.context
    backend.prs[(context.repository, context.branch, context.base)] = 123
    if existing_comment:
        backend.comments[7] = Comment(7, True, "RED\n<!-- ydbdoc-current-qa -->")
    original_comments = backend.comments.copy()
    qa = QAReporter(
        backend,
        publisher,
        lambda: ReportContext(SOURCE, TARGET, None),
        lambda: (),
        verification_context=context,
    )
    with pytest.raises(PublicationError):
        qa.update_current_pr(
            mode=Mode.DOC_VERIFY,
            pr_number=123,
            branch=snapshot.branch,
            commit_sha=TARGET,
            review=review(Verdict.RED, (Finding(True, "", "", "", "", 0),)),
        )
    assert backend.events == []
    assert backend.comments == original_comments


def test_invalid_final_report_cannot_create_pr_after_branch_push():
    backend, snapshot, candidate, publisher, _ = setup_publication()
    publisher.validate_candidate(snapshot, candidate)
    publisher.publish(snapshot, candidate)
    with pytest.raises(PublicationError):
        reporter(backend, publisher).update_current_pr(
            mode=Mode.DOC_TRANSLATE,
            pr_number=42,
            branch=snapshot.branch,
            commit_sha=COMMIT,
            review=review(Verdict.RED, (Finding(True, "", "", "", "", 0),)),
        )
    assert [e[0] for e in backend.events] == ["commit", "push"]
    assert backend.prs == {}
    assert backend.comments == {}


def test_byte_identical_repair_does_not_skip_report_for_already_published_changes():
    backend, snapshot, candidate, _, _ = setup_publication()
    publisher = GitPublicationAdapter(
        backend,
        lambda s, c: PublicationPlan(
            (FileChange(PATH, b"Old" if s.context.current_head == TARGET else b"New", c.content),),
            (),
        ),
        lambda *a: None,
    )
    publisher.validate_candidate(snapshot, candidate)
    publisher.publish(snapshot, candidate)
    publisher.validate_candidate(snapshot, candidate)
    assert publisher.publish(snapshot, candidate) == COMMIT
    reporter(backend, publisher).update_current_pr(
        mode=Mode.DOC_TRANSLATE,
        pr_number=42,
        branch=snapshot.branch,
        commit_sha=COMMIT,
        review=review(),
    )
    assert [e[0] for e in backend.events] == ["commit", "push", "create_pr", "create_comment"]


@pytest.mark.parametrize(
    "checks,status,word",
    [
        ((), "YELLOW", "не запускалась"),
        (
            (
                CheckResult("doc_verify", COMMIT, "success"),
                CheckResult("build-docs", COMMIT, "pending"),
            ),
            "YELLOW",
            "выполняется",
        ),
        (
            (
                CheckResult("doc_verify", TARGET, "success"),
                CheckResult("build-docs", COMMIT, "success"),
            ),
            "YELLOW",
            "устаревший результат",
        ),
        (
            (
                CheckResult("doc_verify", COMMIT, "success"),
                CheckResult("build-docs", COMMIT, "failure"),
            ),
            "RED",
            "завершилась с ошибкой",
        ),
        (
            (
                CheckResult("doc_verify", COMMIT, "success"),
                CheckResult("build-docs", COMMIT, "success"),
            ),
            "GREEN",
            "успешно",
        ),
    ],
)
def test_readiness_requires_both_success_on_exact_current_head(checks, status, word):
    readiness = merge_readiness(COMMIT, checks)
    assert readiness.status == status
    assert word in readiness.reason
    icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}[status]
    report = render_report(review(), COMMIT, ReportContext(SOURCE, TARGET, None), checks)
    assert report.startswith(f"{icon} {status}")
    if status == "YELLOW":
        assert "После завершения проверок повторно запустите `doc_verify`" in report
