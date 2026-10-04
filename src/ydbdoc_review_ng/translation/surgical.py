"""Surgical update of an existing target when the source delta is small.

Whole-file translation remains the fallback when the existing target cannot be
aligned with the source before/after versions.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import Enum

_LINK_DESTINATION = re.compile(rb"\]\(([^)\s]+)")


class SurgicalMode(str, Enum):
    UNIQUE_REPLACEMENTS = "unique_replacements"
    HUNKS = "hunks"
    WHOLE_FILE = "whole_file"


@dataclass(frozen=True, slots=True)
class SurgicalHunk:
    source_after: bytes
    existing_target_fragment: bytes
    target_span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class SurgicalPlan:
    mode: SurgicalMode
    patched_target: bytes | None = None
    hunks: tuple[SurgicalHunk, ...] = ()


def _affix_replacement(before_line: bytes, after_line: bytes) -> tuple[bytes, bytes] | None:
    if before_line == after_line:
        return None
    prefix = 0
    limit = min(len(before_line), len(after_line))
    while prefix < limit and before_line[prefix] == after_line[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(before_line) - prefix
        and suffix < len(after_line) - prefix
        and before_line[-1 - suffix] == after_line[-1 - suffix]
    ):
        suffix += 1
    old_end = len(before_line) - suffix
    new_end = len(after_line) - suffix
    old, new = before_line[prefix:old_end], after_line[prefix:new_end]
    if not old or not new:
        return None
    return old, new


def _dest_path(destination: bytes) -> bytes:
    return destination.split(b"#", 1)[0].split(b"?", 1)[0]


def _changed_link_dest_pairs(
    source_before: bytes, source_after: bytes
) -> tuple[tuple[bytes, bytes], ...] | None:
    before_lines = source_before.splitlines(keepends=True)
    after_lines = source_after.splitlines(keepends=True)
    if len(before_lines) != len(after_lines):
        return None
    mapping: dict[bytes, bytes] = {}
    for before_line, after_line in zip(before_lines, after_lines, strict=True):
        if before_line == after_line:
            continue
        olds = _LINK_DESTINATION.findall(before_line)
        news = _LINK_DESTINATION.findall(after_line)
        if len(olds) != len(news) or not olds:
            return None
        rebuilt = before_line
        for old, new in zip(olds, news, strict=True):
            old_path, new_path = _dest_path(old), _dest_path(new)
            if old_path == new_path:
                continue
            if old_path in mapping and mapping[old_path] != new_path:
                return None
            mapping[old_path] = new_path
            rebuilt = rebuilt.replace(old_path, new_path, 1)
        if rebuilt != after_line:
            return None
    if not mapping:
        return None
    pairs = tuple(mapping.items())
    if apply_unique_replacements(source_before, pairs) != source_after:
        return None
    return pairs


def collect_unique_replacements(
    source_before: bytes, source_after: bytes
) -> tuple[tuple[bytes, bytes], ...] | None:
    if source_before == source_after:
        return ()
    dest_pairs = _changed_link_dest_pairs(source_before, source_after)
    if dest_pairs is not None:
        return dest_pairs
    before_lines = source_before.splitlines(keepends=True)
    after_lines = source_after.splitlines(keepends=True)
    if len(before_lines) != len(after_lines):
        return None
    counts: dict[tuple[bytes, bytes], int] = {}
    for before_line, after_line in zip(before_lines, after_lines, strict=True):
        pair = _affix_replacement(before_line, after_line)
        if pair is None:
            if before_line != after_line:
                return None
            continue
        counts[pair] = counts.get(pair, 0) + 1
    if not counts:
        return None
    for old, new in counts:
        if source_before.count(old) != counts[(old, new)]:
            return None
        if any(other_old != old and old in other_old for other_old, _new in counts):
            return None
    pairs = tuple(counts)
    if apply_unique_replacements(source_before, pairs) != source_after:
        return None
    return pairs


def apply_unique_replacements(
    text: bytes, pairs: tuple[tuple[bytes, bytes], ...]
) -> bytes | None:
    patched = text
    for old, new in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        if old not in patched:
            if new not in patched:
                return None
            continue
        patched = patched.replace(old, new)
    return patched


def _already_applied(text: bytes, pairs: tuple[tuple[bytes, bytes], ...]) -> bool:
    return all(text.count(old) == 0 and text.count(new) > 0 for old, new in pairs)


def _enclosing_line(text: bytes, position: int) -> tuple[int, int]:
    start = text.rfind(b"\n", 0, position) + 1
    end = text.find(b"\n", position)
    if end < 0:
        return start, len(text)
    return start, end + 1


def _locate_target_span(source_before_hunk: bytes, existing_target: bytes) -> tuple[int, int] | None:
    destinations = _LINK_DESTINATION.findall(source_before_hunk)
    for destination in sorted(set(destinations), key=len, reverse=True):
        if existing_target.count(destination) == 1:
            return _enclosing_line(existing_target, existing_target.find(destination))
    return None


def _changed_line_hunks(
    source_before: bytes, source_after: bytes
) -> tuple[tuple[bytes, bytes], ...]:
    before_lines = source_before.splitlines(keepends=True)
    after_lines = source_after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    hunks: list[tuple[bytes, bytes]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append((b"".join(before_lines[i1:i2]), b"".join(after_lines[j1:j2])))
    return tuple(hunks)


def plan_surgical_update(
    source_before: bytes,
    source_after: bytes,
    existing_target: bytes,
) -> SurgicalPlan:
    if not source_before or not source_after or not existing_target:
        return SurgicalPlan(SurgicalMode.WHOLE_FILE)
    if source_before == source_after:
        return SurgicalPlan(SurgicalMode.WHOLE_FILE)
    pairs = collect_unique_replacements(source_before, source_after)
    if pairs is not None:
        patched = apply_unique_replacements(existing_target, pairs)
        if patched is None and _already_applied(existing_target, pairs):
            patched = existing_target
        if patched is not None:
            return SurgicalPlan(SurgicalMode.UNIQUE_REPLACEMENTS, patched_target=patched)
    line_hunks = _changed_line_hunks(source_before, source_after)
    if line_hunks and all(not before for before, after in line_hunks) and all(
        after for _before, after in line_hunks
    ):
        inserted = b"".join(after for _before, after in line_hunks)
        end = len(existing_target)
        return SurgicalPlan(
            SurgicalMode.HUNKS,
            hunks=(
                SurgicalHunk(
                    source_after=inserted,
                    existing_target_fragment=b"",
                    target_span=(end, end),
                ),
            ),
        )
    hunks: list[SurgicalHunk] = []
    for before_hunk, after_hunk in _changed_line_hunks(source_before, source_after):
        if not after_hunk:
            return SurgicalPlan(SurgicalMode.WHOLE_FILE)
        span = _locate_target_span(before_hunk or after_hunk, existing_target)
        if span is None:
            return SurgicalPlan(SurgicalMode.WHOLE_FILE)
        hunks.append(
            SurgicalHunk(
                source_after=after_hunk,
                existing_target_fragment=existing_target[span[0] : span[1]],
                target_span=span,
            )
        )
    if not hunks:
        return SurgicalPlan(SurgicalMode.WHOLE_FILE)
    spans = [item.target_span for item in hunks]
    if len({item[0] for item in spans}) != len(spans):
        return SurgicalPlan(SurgicalMode.WHOLE_FILE)
    return SurgicalPlan(SurgicalMode.HUNKS, hunks=tuple(hunks))
