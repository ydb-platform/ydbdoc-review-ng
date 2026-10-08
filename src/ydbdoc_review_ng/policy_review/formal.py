"""Conservative line checks; PR content is never executed or fetched from external URLs."""

from __future__ import annotations

import re

from ydbdoc_review_ng.policy_review.types import PRIORITIES, Finding, ReviewFile

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^ {0,3}(?:[-*+] |\d+[.)] )")
_HEADING = re.compile(r"^ {0,3}#{1,6} ")


def formal_findings(file: ReviewFile) -> tuple[Finding, ...]:
    if file.after is None or not file.path.endswith(".md"):
        return ()
    lines = file.after.splitlines()
    result: list[Finding] = []

    def add(rule: str, index: int, why: str, fix: str) -> None:
        result.append(Finding(rule, file.path, index + 1, lines[index], why, fix, PRIORITIES[rule]))

    visible: list[bool] = []
    fence: tuple[str, int] | None = None
    frontmatter = bool(lines and lines[0] == "---")
    for index, line in enumerate(lines):
        in_frontmatter = frontmatter
        if frontmatter and index > 0 and line == "---":
            frontmatter = False
        match = None if in_frontmatter else _FENCE.match(line)
        is_fence = match is not None
        if match is not None:
            marker, info = match.groups()
            if fence is None:
                fence = (marker[0], len(marker))
                if info.strip().split(maxsplit=1)[:1] == ["sql"]:
                    add("FORMAT.YQL", index, "SQL-блок должен иметь диалект yql.",
                        "Замените метку языка sql на yql.")
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not info.strip():
                fence = None
        visible.append(fence is None and not is_fence and not in_frontmatter)
        # Two spaces are an allowed Markdown hard break in the normative FORMAT_RULES.
        suffix = line[len(line.rstrip(" \t")):]
        if suffix and suffix != "  ":
            add("FORMAT.MD009", index, "Пробельные символы в конце строки.",
                "Удалите пробелы и табуляции в конце строки.")
    for index, line in enumerate(lines):
        if not visible[index] or not _LIST.match(line):
            continue
        if index and visible[index - 1] and lines[index - 1].strip() and not (
            _LIST.match(lines[index - 1]) or _HEADING.match(lines[index - 1])
            or lines[index - 1].startswith((" ", "\t"))
        ):
            add("FORMAT.MD032", index, "Перед списком отсутствует пустая строка.",
                "Добавьте пустую строку перед первым элементом списка.")
        if index + 1 < len(lines) and visible[index + 1] and lines[index + 1].strip() and not (
            _LIST.match(lines[index + 1]) or _HEADING.match(lines[index + 1])
            or lines[index + 1].startswith((" ", "\t"))
        ):
            add("FORMAT.MD032", index, "После списка отсутствует пустая строка.",
                "Добавьте пустую строку после последнего элемента списка.")
    return tuple(result)


def new_findings(file: ReviewFile, findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    """Compare violations in both versions, preserving full-page inspection context."""
    if file.before is None:
        return findings
    before = formal_findings(ReviewFile(file.path, None, file.before))
    existing: dict[tuple[str, str, str], int] = {}
    for finding in before:
        key = (finding.rule_id, finding.quote, finding.explanation)
        existing[key] = existing.get(key, 0) + 1
    result: list[Finding] = []
    for finding in findings:
        key = (finding.rule_id, finding.quote, finding.explanation)
        if existing.get(key, 0):
            existing[key] -= 1
        else:
            result.append(finding)
    return tuple(result)
