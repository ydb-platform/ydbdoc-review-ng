"""Concise public QA reports and one current bot comment per translation PR."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ydbdoc_review_ng.domain import GitSha, Mode, RepoPath
from ydbdoc_review_ng.publication import GitPublicationAdapter, PublicationContext, PublicationError
from ydbdoc_review_ng.quality import Finding, QualityReviewResult, Verdict

QA_MARKER = "<!-- ydbdoc-current-qa -->"
_MAX_REPORTED_FILES = 10
_STATUS_ICONS = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    head_sha: GitSha
    status: str


@dataclass(frozen=True, slots=True)
class Readiness:
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProbableDuplicate:
    new_target_path: RepoPath
    existing_target_path: RepoPath


def merge_readiness(head: GitSha, checks: tuple[CheckResult, ...]) -> Readiness:
    reasons = []
    failed = False
    waiting = False
    for name in ("doc_verify", "build-docs"):
        named = tuple(check for check in checks if check.name == name)
        current = tuple(check for check in named if check.head_sha == head)
        if not current:
            reasons.append(
                f"{name}: устаревший результат" if named else f"{name}: не запускалась"
            )
            waiting = True
        elif any(
            check.status in {"failure", "cancelled", "timed_out", "error"} for check in current
        ):
            reasons.append(f"{name}: завершилась с ошибкой")
            failed = True
        elif any(check.status != "success" for check in current):
            reasons.append(f"{name}: выполняется")
            waiting = True
        else:
            reasons.append(f"{name}: успешно")
    return Readiness("RED" if failed else "YELLOW" if waiting else "GREEN", "; ".join(reasons))


@dataclass(frozen=True, slots=True)
class ReportContext:
    source_sha: GitSha
    target_sha: GitSha
    job_cost_rub: Decimal | None
    probable_duplicates: tuple[ProbableDuplicate, ...] = ()


def _line(value: str) -> str:
    return " ".join(value.split())


def _finding_lines(review: QualityReviewResult) -> list[str]:
    findings = review.final.findings
    for finding in findings:
        if (
            type(finding.target_line) is not int
            or finding.target_line <= 0
            or any(
                type(value) is not str or not value.strip()
                for value in (
                    finding.target_path,
                    finding.searchable_snippet,
                    finding.reason,
                    finding.expected_correction,
                )
            )
        ):
            raise PublicationError("invalid_finding")

    lines = ["### Что исправить"]
    by_path: dict[str, list[Finding]] = {}
    for finding in findings:
        by_path.setdefault(_line(finding.target_path)[:240], []).append(finding)
    shown_paths = tuple(by_path)[:_MAX_REPORTED_FILES]
    for path in shown_paths:
        grouped = by_path[path]
        finding = grouped[0]
        lines.append(f"**`{path}`**")
        lines.append(
            f"- строка {finding.target_line}, `"
            f"{_line(finding.searchable_snippet)[:80]}`: "
            f"{_line(finding.reason)[:160]} Исправление: "
            f"{_line(finding.expected_correction)[:160]}"
        )
        omitted_in_file = len(grouped) - 1
        if omitted_in_file:
            lines.append(f"- И ещё {omitted_in_file} замечаний в этом файле.")
    omitted_files = len(by_path) - len(shown_paths)
    if omitted_files:
        lines.append(f"Ещё {omitted_files} затронутых файлов не показаны.")
    if not findings:
        lines.append("- Проверка вернула RED без конкретного замечания.")
    return lines


def render_report(
    review: QualityReviewResult,
    commit_sha: GitSha,
    context: ReportContext,
    checks: tuple[CheckResult, ...],
) -> str:
    readiness = merge_readiness(commit_sha, checks)
    status = "RED" if review.final.verdict is Verdict.RED else readiness.status
    if status == "GREEN" and context.probable_duplicates:
        status = "YELLOW"
    cost = "неизвестна" if context.job_cost_rub is None else f"{context.job_cost_rub} RUB"
    lines = [
        f"{_STATUS_ICONS[status]} {status}",
        f"Стоимость запуска: {cost}",
    ]
    if review.final.verdict is Verdict.RED:
        lines.extend(_finding_lines(review))
        lines.extend(
            (
                "### Как продолжить",
                (
                    "Оставьте комментарий, начинающийся с `/ydbdoc continue`, "
                    "добавьте нужный контекст следующими строками и поставьте "
                    "label `doc_continue`."
                ),
            )
        )
    elif context.probable_duplicates:
        lines.append("### Возможный дубликат")
        for warning in context.probable_duplicates:
            lines.append(
                f"- Создана новая статья `{warning.new_target_path.value}`, но, возможно, "
                f"она дублирует существующую `{warning.existing_target_path.value}`."
            )
        if readiness.status != "GREEN":
            lines.append(f"Проверки: {readiness.reason}.")
        lines.append(
            "Разберитесь вручную с возможным дубликатом, затем повторно запустите `doc_verify`."
        )
    elif readiness.status == "GREEN":
        lines.append("Перевод проверен. Исправления не требуются.")
    elif readiness.status == "YELLOW":
        lines.extend(
            (
                f"Проверки: {readiness.reason}.",
                "После завершения проверок повторно запустите `doc_verify`.",
            )
        )
    else:
        lines.extend(
            (
                "### Что исправить",
                f"- Обязательные проверки: {readiness.reason}.",
                "Исправьте ошибку проверки и повторно запустите `doc_verify`.",
            )
        )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Comment:
    id: int
    authored_by_publisher: bool
    body: str


class CommentBackend(Protocol):
    def find_pr(self, repository: str, branch: str, base: str, /) -> int | None: ...
    def list_comments(self, pr_number: int, /) -> tuple[Comment, ...]: ...
    def create_comment(self, pr_number: int, body: str, /) -> None: ...
    def update_comment(self, pr_number: int, comment_id: int, body: str, /) -> None: ...


class QAReporter:
    def __init__(
        self,
        backend: CommentBackend,
        publisher: GitPublicationAdapter,
        report_context: Callable[[], ReportContext],
        checks: Callable[[], tuple[CheckResult, ...]],
        *,
        verification_context: PublicationContext | None = None,
        current_head: Callable[[], GitSha | None] | None = None,
    ) -> None:
        self._backend = backend
        self._publisher = publisher
        self._report_context = report_context
        self._checks = checks
        self._verification_context = verification_context
        self._current_head = current_head

    def update_current_pr(
        self,
        *,
        mode: Mode,
        pr_number: int,
        branch: str,
        commit_sha: GitSha,
        review: QualityReviewResult,
    ) -> None:
        if mode is Mode.DOC_TRANSLATE and self._publisher.noop:
            return
        context = self._publisher.context or self._verification_context
        if (
            context is None
            or context.branch != branch
            or context.repository != "ydb-platform/ydb"
            or context.base != context.source_base
            or context.current_head != commit_sha
        ):
            raise PublicationError("report_context_mismatch")
        try:
            body = render_report(review, commit_sha, self._report_context(), self._checks())
            body += "\n" + QA_MARKER
            if self._publisher.context is not None and not self._publisher.noop:
                if self._current_head is not None and self._current_head() != commit_sha:
                    raise PublicationError("report_head_changed")
                number = self._publisher.ensure_pr(commit_sha)
            else:
                number = self._backend.find_pr(context.repository, branch, context.base)
            if number is None:
                raise PublicationError("translation_pr_missing")
            existing = next(
                (
                    comment
                    for comment in self._backend.list_comments(number)
                    if comment.authored_by_publisher and QA_MARKER in comment.body
                ),
                None,
            )
            if self._current_head is not None and self._current_head() != commit_sha:
                raise PublicationError("report_head_changed")
            if existing is None:
                self._backend.create_comment(number, body)
            else:
                self._backend.update_comment(number, existing.id, body)
            # The SHA-labelled comment may have been written during a ref race.
            # Do not acknowledge reporting success or allow checkpoint handoff.
            if self._current_head is not None and self._current_head() != commit_sha:
                raise PublicationError("report_head_changed")
        except Exception:  # noqa: BLE001 - backend exceptions can contain credentials.
            raise PublicationError("report_failed") from None
