"""Hard publication gates after thin whole-file translation.

Fail closed: a file that fails these gates is not reader-facing product and must
not be soft-published for critic to “figure out later”.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from ydbdoc_review_ng.translation.split_backtick import count_split_backtick_identifiers

# Continuous source-locale letters (RU) in an EN target.
_CYRILLIC_RUN = re.compile(r"[А-Яа-яЁё]{3,}")
_INCLUDE = re.compile(
    r"\{%\s*include\s+\[[^\]]*\]\(([^)]+)\)\s*%\}",
    re.IGNORECASE,
)
_ATX_HEADING = re.compile(r"^#{1,6}(?:\s|$)")


@dataclass(frozen=True, slots=True)
class PublicationGateFailure:
    code: str
    detail: str


def leftover_source_locale_runs(text: str, /, *, source_locale: str) -> tuple[str, ...]:
    """Return leftover source-script runs that must not ship in the target."""
    if source_locale != "ru":
        return ()
    return tuple(sorted(set(_CYRILLIC_RUN.findall(text))))


def include_targets(text: str, /) -> tuple[str, ...]:
    return tuple(_INCLUDE.findall(text))


def resolved_include_path(target_path: str, destination: str) -> str:
    """Normalize a relative include destination against the including page."""
    resolved = PurePosixPath(PurePosixPath(target_path).parent / destination)
    parts: list[str] = []
    for part in resolved.parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def missing_relative_includes(
    text: str,
    /,
    *,
    target_path: str,
    available_paths: set[str],
    exists: Callable[[str], bool] | None = None,
) -> tuple[str, ...]:
    """Relative include destinations absent from the resulting target tree.

    ``available_paths`` is the in-flight published/deleted overlay. ``exists``
    answers whether an untouched path already exists on the target snapshot, so
    a page is not nulled for an include that main already has.
    """
    missing: list[str] = []
    for raw in include_targets(text):
        destination = raw.strip()
        if not destination or destination.startswith(("/", "http://", "https://")):
            continue
        normalized = resolved_include_path(target_path, destination)
        if normalized in available_paths:
            continue
        if exists is not None and exists(normalized):
            continue
        missing.append(destination)
    return tuple(missing)


def _heading_blank_line_problems(text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    problems: list[str] = []
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not _ATX_HEADING.match(line):
            continue
        missing_above = index > 0 and lines[index - 1] != ""
        missing_below = index + 1 < len(lines) and lines[index + 1] != ""
        if missing_above or missing_below:
            problems.append(line.strip()[:80])
    return tuple(problems)


def _is_prose_line(line: str) -> bool:
    stripped = line.lstrip()
    if not stripped:
        return False
    if _ATX_HEADING.match(line):
        return False
    if stripped.startswith(("```", "{%", "|", ">")):
        return False
    if stripped.startswith(("- ", "* ", "+ ")):
        return False
    if len(stripped) >= 2 and stripped[0].isdigit() and stripped[1] in ".)":
        return False
    return (
        stripped[0].isalpha()
        or stripped.startswith(("**", "[", "`", "_", "*", '"', "'"))
    )


def join_soft_wrapped_prose(draft: bytes, /) -> bytes:
    """Join adjacent prose lines into one paragraph (YFM soft-break repair)."""
    if type(draft) is not bytes:
        raise TypeError("draft must be exact bytes")
    text = draft.decode("utf-8")
    lines = text.splitlines()
    in_fence = False
    outside: list[bool] = []
    for line in lines:
        stripped = line.lstrip()
        outside.append(not in_fence)
        if stripped.startswith("```"):
            in_fence = not in_fence
    if not lines:
        return draft if draft.endswith(b"\n") or not draft else draft + b"\n"
    merged: list[str] = [lines[0]]
    for index in range(1, len(lines)):
        previous = merged[-1]
        current = lines[index]
        if (
            outside[index - 1]
            and outside[index]
            and _is_prose_line(previous)
            and _is_prose_line(current)
        ):
            merged[-1] = previous.rstrip() + " " + current.lstrip()
        else:
            merged.append(current)
    body = "\n".join(merged)
    if draft.endswith(b"\n") or body:
        body += "\n"
    return body.encode("utf-8")


def _unlabeled_fence_openers(text: str) -> int:
    in_fence = False
    unlabeled = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("```"):
            continue
        if in_fence:
            in_fence = False
            continue
        info = stripped[3:].strip()
        if not info:
            unlabeled += 1
        in_fence = True
    return unlabeled


def check_publication_gates(
    target: bytes,
    /,
    *,
    source_locale: str,
    target_path: str,
    available_paths: set[str] | None = None,
    exists: Callable[[str], bool] | None = None,
) -> tuple[PublicationGateFailure, ...]:
    """Return zero or more hard gate failures for one translated file."""
    if type(target) is not bytes:
        raise TypeError("target must be exact bytes")
    try:
        text = target.decode("utf-8")
    except UnicodeDecodeError:
        return (PublicationGateFailure("utf8_invalid", "target is not valid UTF-8"),)

    failures: list[PublicationGateFailure] = []
    leftover = leftover_source_locale_runs(text, source_locale=source_locale)
    if leftover:
        sample = ", ".join(leftover[:5])
        failures.append(
            PublicationGateFailure(
                "source_locale_echo",
                f"target still contains source-locale prose ({len(leftover)} runs): {sample}",
            )
        )
    mangled = count_split_backtick_identifiers(target)
    if mangled:
        failures.append(
            PublicationGateFailure(
                "split_backtick_identifiers",
                f"target still contains {mangled} split-backtick identifier runs",
            )
        )
    if available_paths is not None:
        missing = missing_relative_includes(
            text,
            target_path=target_path,
            available_paths=available_paths,
            exists=exists,
        )
        if missing:
            failures.append(
                PublicationGateFailure(
                    "missing_include_target",
                    "relative include targets missing: " + ", ".join(missing[:8]),
                )
            )
    headings = _heading_blank_line_problems(text)
    if headings:
        failures.append(
            PublicationGateFailure(
                "heading_blank_lines",
                "ATX headings must be surrounded by blank lines: " + headings[0],
            )
        )
    unlabeled = _unlabeled_fence_openers(text)
    if unlabeled:
        failures.append(
            PublicationGateFailure(
                "unlabeled_fence_opener",
                f"target has {unlabeled} opening code fences without a language",
            )
        )
    return tuple(failures)
