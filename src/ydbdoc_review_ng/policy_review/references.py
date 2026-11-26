"""Resolve local Markdown context without following external URLs or executing content."""

import posixpath
import re
import urllib.parse

from ydbdoc_review_ng.policy_review.types import ROOT


def markdown_targets(path: str, content: str) -> tuple[str, ...]:
    targets = []
    for target in re.findall(r"\]\(([^\s)]+\.md(?:#[^\s)]*)?)\)", content):
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), parsed.path))
        if resolved.startswith(ROOT) and resolved not in targets:
            targets.append(resolved)
    return tuple(targets)
