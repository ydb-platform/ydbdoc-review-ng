"""Markdown heading-anchor inventory used by translation scope checks."""

from __future__ import annotations

import re

_HEADING = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")
_EXPLICIT = re.compile(r"\{#([A-Za-z0-9_.:-]+)\}\s*$")


def markdown_anchors(content: bytes, /) -> frozenset[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return frozenset()
    anchors: set[str] = set()
    for match in _HEADING.finditer(text):
        heading = match.group(1)
        explicit = _EXPLICIT.search(heading)
        if explicit is not None:
            anchors.add(explicit.group(1))
            continue
        plain = re.sub(r"[`*_~\[\]()]", "", heading).strip().casefold()
        slug = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
        if slug:
            anchors.add(slug)
    return frozenset(anchors)
