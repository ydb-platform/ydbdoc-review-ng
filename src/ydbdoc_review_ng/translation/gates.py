"""Technical publication checks, without style or script-based rejection.

Heading case, whitespace, fence language and natural-language quality are not
reasons to discard a translation. Semantic review evaluates translation quality.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from markdown_it import MarkdownIt

_INCLUDE = re.compile(
    r"\{%\s*include\s+\[[^\]]*\]\(([^)]+)\)\s*%\}",
    re.IGNORECASE,
)
_ATX_HEADING_PREFIX = re.compile(r"^#{1,6}(?:\s|$)")
@dataclass(frozen=True, slots=True)
class PublicationGateFailure:
    code: str
    detail: str


def include_targets(text: str, /) -> tuple[str, ...]:
    # Parse Markdown containers instead of guessing indentation/fence boundaries.
    # Leave link syntax as text so the include directive remains intact. Inline
    # code and escapes still use CommonMark rules, scoped to each paragraph.
    parser = MarkdownIt("commonmark", {"html": False}).disable(
        ["link", "image", "autolink", "emphasis", "entity"]
    )
    targets: list[str] = []
    for token in parser.parse(text):
        if token.type != "inline":
            continue
        prose = "".join(
            child.content if child.type == "text" else "\n"
            for child in token.children or ()
        )
        targets.extend(_INCLUDE.findall(prose))
    return tuple(targets)


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


def _is_prose_line(line: str) -> bool:
    stripped = line.lstrip()
    if not stripped:
        return False
    if _ATX_HEADING_PREFIX.match(line):
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


def check_publication_gates(
    target: bytes,
    /,
    *,
    source_locale: str,
    target_path: str,
    available_paths: set[str] | None = None,
    exists: Callable[[str], bool] | None = None,
    previous_target: bytes | None = None,
) -> tuple[PublicationGateFailure, ...]:
    """Return zero or more hard gate failures for one translated file."""
    if type(target) is not bytes:
        raise TypeError("target must be exact bytes")
    try:
        text = target.decode("utf-8")
    except UnicodeDecodeError:
        return (PublicationGateFailure("utf8_invalid", "target is not valid UTF-8"),)

    failures: list[PublicationGateFailure] = []
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
    return tuple(failures)
