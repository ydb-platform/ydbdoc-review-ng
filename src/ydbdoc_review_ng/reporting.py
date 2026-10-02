"""Concise public QA reports and one current bot comment per translation PR."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ydbdoc_review_ng.config import (
    MAX_DEPENDENCY_FILES_VARIABLE,
    MAX_SOURCE_CHARACTERS_VARIABLE,
)
from ydbdoc_review_ng.domain import GitSha, Mode, RepoPath
from ydbdoc_review_ng.publication import GitPublicationAdapter, PublicationContext, PublicationError
from ydbdoc_review_ng.quality import Finding, QualityReviewResult, Verdict

QA_MARKER = "<!-- ydbdoc-current-qa -->"
TRANSLATION_LINK_MARKER = "<!-- ydbdoc-translation-pr -->"
SCOPE_FAILURE_MARKER = "<!-- ydbdoc-scope-failure -->"
CLASSIFICATION_MARKER = "<!-- ydbdoc-source-classification -->"
_MAX_REPORTED_FINDINGS = 25
_STATUS_ICONS = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}

_SCOPE_FAILURE_MESSAGES = {
    "dependency_file_limit_exceeded": (
        "число файлов в группе перевода превышает лимит",
        "файлов",
        MAX_DEPENDENCY_FILES_VARIABLE,
    ),
    "source_character_limit_exceeded": (
        "объём исходного текста превышает лимит",
        "символов",
        MAX_SOURCE_CHARACTERS_VARIABLE,
    ),
}
_PLAN_FAILURE_MESSAGES = {
    "translation_plan_direction_missing": (
        "изменён локализованный файл, но для него не построено направление перевода"
    ),
    "translation_plan_localized_file_unsupported": (
        "изменённый локализованный файл имеет неподдерживаемый тип операции"
    ),
    "translation_plan_markdown_missing": (
        "изменённый Markdown-файл отсутствует в плане перевода"
    ),
    "translation_plan_markdown_delete_unsupported": (
        "удаление или tombstone Markdown нельзя безопасно завершить без синхронизации TOC"
    ),
    "translation_plan_toc_uncovered": (
        "изменённый TOC не получил доказанного результата синхронизации"
    ),
    "translation_plan_toc_postcondition_missing": (
        "для изменённого TOC не удалось построить точное ожидаемое содержимое"
    ),
    "translation_plan_toc_source_snapshot_missing": (
        "для изменённого TOC отсутствует закреплённый source base или source head"
    ),
    "translation_plan_toc_delta_unsupported": (
        "изменение TOC содержит пока неподдерживаемую операцию кроме добавления страниц"
    ),
    "translation_plan_toc_delta_uncovered": (
        "изменение TOC затрагивает страницу, отсутствующую в плане перевода"
    ),
    "translation_plan_toc_operation_unsupported": (
        "операция над TOC пока не поддерживается безопасным планировщиком"
    ),
    "translation_plan_status_operation_mismatch": (
        "статус файла противоречит запланированной операции"
    ),
    "translation_plan_rename_crosses_policy_boundary": (
        "переименование пересекает границу локали или типа файла"
    ),
    "translation_plan_target_collision": (
        "автоматический перевод конфликтует с явным изменением target-файла"
    ),
    "translation_plan_candidate_output_missing": (
        "в итоговом кандидате отсутствует обязательный результат перевода"
    ),
    "translation_plan_candidate_delete_missing": (
        "в итоговом кандидате отсутствует обязательное удаление"
    ),
    "translation_plan_candidate_rename_missing": (
        "в итоговом кандидате отсутствует одна из сторон переименования"
    ),
}


@dataclass(frozen=True, slots=True)
class ProbableDuplicate:
    new_target_path: RepoPath
    existing_target_path: RepoPath


@dataclass(frozen=True, slots=True)
class ReportContext:
    source_sha: GitSha
    target_sha: GitSha
    job_cost_rub: Decimal | None
    probable_duplicates: tuple[ProbableDuplicate, ...] = ()
    source_pr_number: int | None = None


def _line(value: str) -> str:
    return " ".join(value.split())


def _finding_lines(review: QualityReviewResult) -> list[str]:
    findings = review.final.findings
    for finding in findings:
        if type(finding.target_path) is not str or not finding.target_path.strip():
            raise PublicationError("invalid_finding")
        if type(finding.reason) is not str or not finding.reason.strip():
            raise PublicationError("invalid_finding")
        if type(finding.expected_correction) is not str or not finding.expected_correction.strip():
            raise PublicationError("invalid_finding")
        if finding.target_line is not None and (
            type(finding.target_line) is not int or finding.target_line <= 0
        ):
            raise PublicationError("invalid_finding")
        if finding.searchable_snippet is not None and (
            type(finding.searchable_snippet) is not str or not finding.searchable_snippet.strip()
        ):
            raise PublicationError("invalid_finding")
        # Missing/unreviewed targets may legally omit line + snippet (§4.2 / §7).
        if (finding.target_line is None) != (finding.searchable_snippet is None):
            raise PublicationError("invalid_finding")

    lines = ["### Что исправить"]
    by_path: dict[str, list[Finding]] = {}
    for finding in findings:
        by_path.setdefault(_line(finding.target_path)[:240], []).append(finding)
    shown = 0
    omitted = 0
    for path, grouped in by_path.items():
        visible: list[Finding] = []
        for finding in grouped:
            if shown < _MAX_REPORTED_FINDINGS:
                visible.append(finding)
                shown += 1
            else:
                omitted += 1
        if not visible:
            continue
        lines.append(f"**`{path}`**")
        for finding in visible:
            if finding.target_line is None:
                lines.append(
                    f"- файл целиком: {_line(finding.reason)[:160]} Исправление: "
                    f"{_line(finding.expected_correction)[:160]}"
                )
            else:
                lines.append(
                    f"- строка {finding.target_line}, `"
                    f"{_line(finding.searchable_snippet)[:80]}`: "
                    f"{_line(finding.reason)[:160]} Исправление: "
                    f"{_line(finding.expected_correction)[:160]}"
                )
    if omitted:
        lines.append(f"Ещё {omitted} замечаний не показаны.")
    if not findings:
        lines.append(f"- Проверка вернула {review.final.verdict.value} без конкретного замечания.")
    return lines


def render_report(
    review: QualityReviewResult,
    context: ReportContext,
) -> str:
    # The QA comment is the semantic translation verdict. Repository build and
    # merge-readiness checks have their own GitHub UI and must never change an
    # arbiter verdict or delay its publication.
    # §0.4 / §4.2 / §7: published color is the arbiter verdict only.
    # Probable duplicates stay a separate advisory note under GREEN.
    status = review.final.verdict.value
    cost = "неизвестна" if context.job_cost_rub is None else f"{context.job_cost_rub} RUB"
    lines = [
        f"{_STATUS_ICONS[status]} {status}",
        f"Стоимость запуска: {cost}",
    ]
    if context.source_pr_number is not None:
        lines.append(f"Перевод PR #{context.source_pr_number}")
    if review.final.verdict in (Verdict.YELLOW, Verdict.RED):
        lines.extend(_finding_lines(review))
    if review.final.verdict is Verdict.RED:
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
    elif review.final.verdict is Verdict.YELLOW:
        lines.extend(
            (
                "### Как продолжить",
                (
                    "Проблемы незначительные: поправьте translation branch вручную "
                    "и поставьте label `doc_verify`."
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
        lines.append(
            "Проверьте возможный дубликат вручную."
        )
    elif review.final.verdict is Verdict.GREEN:
        lines.append("Перевод проверен. Исправления не требуются.")
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


def report_classification(
    backend: CommentBackend, source_pr: int, *, reason: str | None = None
) -> None:
    """Keep one publisher-owned source comment for no-op or technical failure."""
    body = (
        (
            "Перевод не требуется.\n\nПричина: " + _line(reason)
            if reason is not None
            else "🔴 Перевод PR не запущен: классификация изменений не удалась.\n\n"
            "После двух попыток сервис модели недоступен или ответ не соответствует "
            "полному набору файлов PR. Повторно добавьте метку `doc_translate`."
        )
        + "\n"
        + CLASSIFICATION_MARKER
    )
    existing = next(
        (
            comment
            for comment in backend.list_comments(source_pr)
            if comment.authored_by_publisher and CLASSIFICATION_MARKER in comment.body
        ),
        None,
    )
    if existing is None:
        backend.create_comment(source_pr, body)
    else:
        backend.update_comment(source_pr, existing.id, body)


class QAReporter:
    def __init__(
        self,
        backend: CommentBackend,
        publisher: GitPublicationAdapter,
        report_context: Callable[[], ReportContext],
        *,
        verification_context: PublicationContext | None = None,
        current_head: Callable[[], GitSha | None] | None = None,
    ) -> None:
        self._backend = backend
        self._publisher = publisher
        self._report_context = report_context
        self._verification_context = verification_context
        self._current_head = current_head

    def report_failure(
        self, source_pr_number: int, diagnostic: str, configured_limit: int | None = None, /
    ) -> None:
        failure = _SCOPE_FAILURE_MESSAGES.get(diagnostic)
        plan_failure = _PLAN_FAILURE_MESSAGES.get(diagnostic)
        if failure is None and plan_failure is None:
            return
        if failure is not None:
            if type(configured_limit) is not int or configured_limit < 0:
                raise ValueError("scope failure requires the configured limit")
            reason, unit, variable = failure
            details = (
                f"Причина: {reason}.\n"
                f"Лимит: {configured_limit} {unit}.\n"
                f"Переменная: `{variable}`.\n\n"
                "Что сделать: уменьшить scope PR или увеличить настройку лимита, "
                "затем повторно добавить метку `doc_translate`."
            )
        else:
            details = (
                f"Причина: {plan_failure}.\n\n"
                "Это защитная остановка: неполный перевод не опубликован. "
                "Не перезапускайте job до исправления планировщика."
            )
        body = f"🔴 Перевод PR не запущен\n\n{details}\n{SCOPE_FAILURE_MARKER}"
        existing = next(
            (
                comment
                for comment in self._backend.list_comments(source_pr_number)
                if comment.authored_by_publisher and SCOPE_FAILURE_MARKER in comment.body
            ),
            None,
        )
        if existing is None:
            self._backend.create_comment(source_pr_number, body)
        else:
            self._backend.update_comment(source_pr_number, existing.id, body)

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
            # REQUIREMENTS §4.2: zero commits + RED → source PR report, no empty PR.
            if review.final.verdict is not Verdict.RED:
                return
            report_context = self._report_context()
            body = render_report(review, report_context)
            body += "\n" + QA_MARKER
            source_pr = report_context.source_pr_number
            if source_pr is None:
                raise PublicationError("report_context_mismatch")
            try:
                existing = next(
                    (
                        comment
                        for comment in self._backend.list_comments(source_pr)
                        if comment.authored_by_publisher and QA_MARKER in comment.body
                    ),
                    None,
                )
                if existing is None:
                    self._backend.create_comment(source_pr, body)
                else:
                    self._backend.update_comment(source_pr, existing.id, body)
            except Exception:  # noqa: BLE001 - backend exceptions can contain credentials.
                raise PublicationError("report_failed") from None
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
            report_context = self._report_context()
            body = render_report(review, report_context)
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
            if mode in {Mode.DOC_TRANSLATE, Mode.DOC_CONTINUE} and report_context.source_pr_number is not None:
                source_pr = report_context.source_pr_number
                source_body = (
                    "Перевод этого PR: "
                    f"https://github.com/{context.repository}/pull/{number}\n"
                    f"{TRANSLATION_LINK_MARKER}"
                )
                source_comments = self._backend.list_comments(source_pr)
                source_link = next(
                    (
                        comment
                        for comment in source_comments
                        if comment.authored_by_publisher
                        and TRANSLATION_LINK_MARKER in comment.body
                    ),
                    None,
                )
                # §7: once a translation PR exists, the one current QA lives there.
                # An earlier zero-commit RED on the source PR must not keep the
                # current-QA marker or contradictory RED next to the PR link.
                source_qa = next(
                    (
                        comment
                        for comment in source_comments
                        if comment.authored_by_publisher
                        and QA_MARKER in comment.body
                        and TRANSLATION_LINK_MARKER not in comment.body
                    ),
                    None,
                )
                if source_link is None and source_qa is not None:
                    self._backend.update_comment(source_pr, source_qa.id, source_body)
                elif source_link is None:
                    self._backend.create_comment(source_pr, source_body)
                else:
                    self._backend.update_comment(source_pr, source_link.id, source_body)
                    if source_qa is not None:
                        self._backend.update_comment(
                            source_pr,
                            source_qa.id,
                            (
                                "Актуальный QA-отчёт опубликован в translation PR: "
                                f"https://github.com/{context.repository}/pull/{number}\n"
                            ),
                        )
            # The SHA-labelled comment may have been written during a ref race.
            # Do not acknowledge reporting success or allow checkpoint handoff.
            if self._current_head is not None and self._current_head() != commit_sha:
                raise PublicationError("report_head_changed")
        except Exception:  # noqa: BLE001 - backend exceptions can contain credentials.
            raise PublicationError("report_failed") from None
