"""Markdown heading-anchor inventory used by translation scope checks."""

from __future__ import annotations

import re

_HEADING = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
_EXPLICIT = re.compile(r"\{#([A-Za-z0-9_.:-]+)\}\s*$")


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
        (len(match.group(1)), _heading_anchor(match.group(2))) for match in _HEADING.finditer(text)
    )


def markdown_anchors(content: bytes, /) -> frozenset[str]:
    return frozenset(
        anchor for _level, anchor in markdown_heading_outline(content) if anchor is not None
    )
