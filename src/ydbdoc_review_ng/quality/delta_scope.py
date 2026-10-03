"""PR-delta scope for critic/arbiter: judge the change, not the whole file."""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ydbdoc_review_ng.quality.types import Finding, Verdict
from ydbdoc_review_ng.quality.workspace import ToolError, ToolErrorReason
from ydbdoc_review_ng.translation.surgical import SurgicalMode, plan_surgical_update

_TOUCH_PAD = 1
_MAX_DELTA_ITEMS = 16


@dataclass(frozen=True, slots=True)
class PairDeltaScope:
    change_class: str
    source_path: str
    target_path: str
    items: tuple[tuple[str, str], ...]
    touched_lines: tuple[int, ...]
    restrict_findings: bool
    pad: int = 0


def _changed_source_items(before: bytes, after: bytes) -> tuple[tuple[str, str], ...]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if len(before_lines) != len(after_lines):
        items: list[tuple[str, str]] = []
        limit = min(len(before_lines), len(after_lines), _MAX_DELTA_ITEMS)
        for left, right in zip(before_lines[:limit], after_lines[:limit], strict=True):
            if left != right:
                items.append((left.decode("utf-8", "replace"), right.decode("utf-8", "replace")))
        return tuple(items)
    items = []
    for left, right in zip(before_lines, after_lines, strict=True):
        if left == right:
            continue
        items.append((left.decode("utf-8", "replace"), right.decode("utf-8", "replace")))
        if len(items) >= _MAX_DELTA_ITEMS:
            break
    return tuple(items)


def _lines_containing(text: bytes, needles: tuple[bytes, ...]) -> tuple[int, ...]:
    found: list[int] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if any(needle and needle in line for needle in needles):
            found.append(index)
    return tuple(found)


def _span_lines(text: bytes, start: int, end: int) -> tuple[int, ...]:
    if start < 0 or end < start:
        return ()
    prefix = text[:start]
    first = prefix.count(b"\n") + 1
    last = text[:end].count(b"\n") + 1
    return tuple(range(first, last + 1))


def build_pair_delta_scope(
    source_before: bytes | None,
    source_after: bytes,
    draft: bytes,
    *,
    source_path: str,
    target_path: str,
) -> PairDeltaScope:
    if source_before is None or source_before == source_after:
        line_count = draft.count(b"\n") + (0 if draft.endswith(b"\n") or not draft else 1)
        if not draft:
            line_count = 0
        return PairDeltaScope(
            "whole_file",
            source_path,
            target_path,
            (),
            tuple(range(1, line_count + 1)),
            False,
            0,
        )
    plan = plan_surgical_update(source_before, source_after, draft)
    items = _changed_source_items(source_before, source_after)
    if plan.mode is SurgicalMode.UNIQUE_REPLACEMENTS:
        needles: list[bytes] = []
        for before_line, after_line in items:
            needles.append(before_line.encode("utf-8"))
            needles.append(after_line.encode("utf-8"))
        dest_needles = _dest_needles(source_before, source_after)
        touched = _lines_containing(draft, dest_needles) or _lines_containing(
            draft, tuple(needles[:8])
        )
        return PairDeltaScope(
            "unique_dest",
            source_path,
            target_path,
            items,
            touched,
            bool(touched),
            0,
        )
    if plan.mode is SurgicalMode.HUNKS:
        touched: list[int] = []
        for hunk in plan.hunks:
            touched.extend(_span_lines(draft, hunk.target_span[0], hunk.target_span[1]))
        unique_touched = tuple(sorted(set(touched)))
        return PairDeltaScope(
            "prose_hunks",
            source_path,
            target_path,
            items,
            unique_touched,
            bool(unique_touched),
            _TOUCH_PAD,
        )
    line_count = draft.count(b"\n") + (0 if draft.endswith(b"\n") or not draft else 1)
    return PairDeltaScope(
        "whole_file",
        source_path,
        target_path,
        items,
        tuple(range(1, max(line_count, 1) + 1)),
        False,
        0,
    )


def _dest_needles(source_before: bytes, source_after: bytes) -> tuple[bytes, ...]:
    from ydbdoc_review_ng.translation.surgical import collect_unique_replacements

    pairs = collect_unique_replacements(source_before, source_after)
    if not pairs:
        return ()
    needles: list[bytes] = []
    for old, new in pairs:
        needles.append(old)
        needles.append(new)
    return tuple(needles)


def format_delta_brief(scopes: Sequence[PairDeltaScope]) -> str:
    if not scopes:
        return ""
    blocks = [
        "Work like a chat editor reviewing one documentation PR.",
        "Judge ONLY the source PR delta and regressions vs previous EN.",
        "Do NOT audit historical sections outside the delta.",
        (
            "Do NOT invent style findings (periods, articles, old issue numbers) "
            "outside touched EN lines."
        ),
        "Critic: if the delta is already in the draft, finish without patches.",
        (
            "Arbiter: default GREEN. Finding only if the delta is missing/wrong or "
            "previous EN outside the delta was damaged."
        ),
    ]
    for scope in scopes:
        blocks.append(
            f"\nCHANGE CLASS: {scope.change_class}\n"
            f"Source: {scope.source_path}\n"
            f"Target: {scope.target_path}\n"
            f"Touched EN line numbers (1-based): {list(scope.touched_lines)}"
        )
        if scope.items:
            blocks.append("Source delta (RU before → RU after):")
            for index, (before, after) in enumerate(scope.items, start=1):
                blocks.append(f"\n### delta {index}\nRU before:\n{before}\n\nRU after:\n{after}")
    return "\n".join(blocks)


def line_in_touched(line: int | None, touched: Sequence[int], *, pad: int = _TOUCH_PAD) -> bool:
    if line is None or not touched:
        return False
    allowed: set[int] = set()
    for item in touched:
        allowed.update(range(max(1, item - pad), item + pad + 1))
    return line in allowed


def filter_out_of_delta_findings(
    findings: tuple[Finding, ...],
    scopes: Mapping[str, PairDeltaScope],
) -> tuple[Finding, ...]:
    kept: list[Finding] = []
    for finding in findings:
        scope = scopes.get(finding.target_path)
        if scope is None or not scope.restrict_findings:
            kept.append(finding)
            continue
        if finding.searchable_snippet is None:
            kept.append(finding)
            continue
        if line_in_touched(finding.target_line, scope.touched_lines, pad=scope.pad):
            kept.append(finding)
    return tuple(kept)


def apply_delta_finding_filter(
    verdict: Verdict,
    findings: tuple[Finding, ...],
    scopes: Mapping[str, PairDeltaScope],
) -> tuple[Verdict, tuple[Finding, ...]]:
    kept = filter_out_of_delta_findings(findings, scopes)
    if kept == findings:
        return verdict, findings
    if not kept:
        return Verdict.GREEN, ()
    if verdict is Verdict.GREEN:
        return Verdict.YELLOW, kept
    return verdict, kept


def delta_touched_guard(original: bytes, touched: Sequence[int], *, pad: int = 0):
    allowed = set()
    for item in touched:
        allowed.update(range(max(1, item - pad), item + pad + 1))

    def after_patch(candidate: bytes) -> None:
        if candidate == original:
            return
        old_lines = original.splitlines()
        new_lines = candidate.splitlines()
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            nearby = {i1, i1 + 1} if tag == "insert" else set(range(i1 + 1, i2 + 1))
            if not nearby.intersection(allowed):
                raise ToolError(
                    ToolErrorReason.INVALID_PATCH,
                    f"patch outside PR delta (source lines {i1 + 1}-{i2})",
                )

    return after_patch
