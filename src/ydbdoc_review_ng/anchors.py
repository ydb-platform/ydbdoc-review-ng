"""Markdown heading-anchor inventory used by translation scope checks."""

from __future__ import annotations

import re

_HEADING = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
_EXPLICIT = re.compile(r"\{#([A-Za-z0-9_.:-]+)\}\s*$")


def _heading_matches(text: str) -> tuple[re.Match[str], ...]:
    opaque: list[tuple[int, int]] = [
        (match.start(), match.end())
        for match in re.finditer(r"<!--[\s\S]*?(?:-->|\Z)", text)
    ]
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    index = 0
    if lines and lines[0].rstrip("\r\n") == "---":
        for probe in range(1, len(lines)):
            if lines[probe].rstrip("\r\n") in {"---", "..."}:
                opaque.append((0, offsets[probe] + len(lines[probe])))
                index = probe + 1
                break
    while index < len(lines):
        body = lines[index].rstrip("\r\n")
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", body)
        if opening is None:
            index += 1
            continue
        marker = opening.group(1)
        closing = re.compile(
            r"^ {0,3}" + re.escape(marker[0]) + r"{" + str(len(marker)) + r",}[ \t]*$"
        )
        probe = index + 1
        while probe < len(lines) and closing.match(lines[probe].rstrip("\r\n")) is None:
            probe += 1
        end = len(text) if probe == len(lines) else offsets[probe] + len(lines[probe])
        opaque.append((offsets[index], end))
        index = probe + 1
    return tuple(
        match
        for match in _HEADING.finditer(text)
        if not any(match.start() < end and match.end() > start for start, end in opaque)
    )


def _heading_anchor(heading: str) -> str | None:
    explicit = _EXPLICIT.search(heading)
    if explicit is not None:
        return explicit.group(1)
    plain = re.sub(r"[`*_~\[\]()]", "", heading).strip().casefold()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-") or None


def markdown_heading_outline(content: bytes, /) -> tuple[tuple[int, str | None], ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    return tuple(
        (len(match.group(1)), _heading_anchor(match.group(2)))
        for match in _heading_matches(text)
    )


def markdown_anchors(content: bytes, /) -> frozenset[str]:
    return frozenset(
        anchor for _level, anchor in markdown_heading_outline(content) if anchor is not None
    )


def markdown_explicit_anchors(content: bytes, /) -> frozenset[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return frozenset()
    anchors = []
    for heading in _heading_matches(text):
        explicit = _EXPLICIT.search(heading.group(2))
        if explicit is not None:
            anchors.append(explicit.group(1))
    return frozenset(anchors)
