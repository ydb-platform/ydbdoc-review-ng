"""Narrow, lossless spacing repair required by the documentation build."""

from __future__ import annotations

import re

from markdown_it import MarkdownIt


def normalize_heading_spacing(draft: bytes, /) -> bytes:
    """Add missing blank lines around top-level ATX headings (MD022).

    Parse complete documents, never individual chunks. Markdown containers,
    code and HTML are left intact; YAML front matter is masked before parsing.
    Existing lines, case, anchors, spacing and line endings are not rewritten.
    """
    text = draft.decode("utf-8")
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text)[:-1]
    if not lines:
        return draft
    parse_lines = list(lines)
    if lines[0].rstrip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].rstrip() in {"---", "..."}), None)
        if end is None:
            return draft
        parse_lines[: end + 1] = ["\n"] * (end + 1)
    boundaries: dict[int, str] = {}
    for token in MarkdownIt("commonmark").parse("".join(parse_lines)):
        if token.type != "heading_open" or token.level != 0 or not token.markup.startswith("#"):
            continue
        assert token.map is not None
        start, end = token.map
        newline = "\r\n" if lines[start].endswith("\r\n") else (
            "\r" if lines[start].endswith("\r") else "\n"
        )
        if start > 0 and lines[start - 1].strip():
            boundaries[start] = newline
        if end < len(lines) and lines[end].strip():
            boundaries[end] = newline
    return "".join(boundaries.get(i, "") + line for i, line in enumerate(lines)).encode("utf-8")
