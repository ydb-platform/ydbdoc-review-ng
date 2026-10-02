"""Optional presentation transfer from an existing target translation.

Old EN/RU is a formatting reference only: which identifier atoms were wrapped in
inline code, and that bare underscores are preferred over Markdown escapes.
When the existing target is absent, the map is empty and apply is a no-op.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from ydbdoc_review_ng.domain import RepoPath, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind, fields_of

_CANONICAL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass(frozen=True, slots=True)
class PresentationStyle:
    token: str
    inline_code: bool


def _canonical_identifier(fragment: bytes) -> str | None:
    try:
        text = fragment.replace(b"\\_", b"_").decode("utf-8")
    except UnicodeDecodeError:
        return None
    if _CANONICAL_IDENTIFIER.fullmatch(text) is None:
        return None
    if "_" not in text and "::" not in text:
        return None
    return text


def build_presentation_map(
    existing_target: bytes | None,
    /,
    *,
    source_snapshot: SnapshotRef,
    source_path: RepoPath,
) -> dict[str, PresentationStyle]:
    """Collect formatting hints from an optional existing target file."""
    if existing_target is None:
        return {}
    if type(existing_target) is not bytes:
        raise TypeError("existing_target must be bytes or None")
    plan = build_markdown_plan(source_snapshot, source_path, existing_target)
    styles: dict[str, PresentationStyle] = {}
    for field in fields_of(plan):
        for region in field.protected_regions:
            fragment = existing_target[region.span.start : region.span.end]
            if region.kind is ProtectedKind.INLINE_CODE:
                if not (
                    fragment.startswith(b"`") and fragment.endswith(b"`") and len(fragment) >= 2
                ):
                    continue
                token = _canonical_identifier(fragment[1:-1])
                if token is None:
                    continue
                styles[token] = PresentationStyle(token, True)
            elif region.kind is ProtectedKind.IDENTIFIER:
                token = _canonical_identifier(fragment)
                if token is None:
                    continue
                styles.setdefault(token, PresentationStyle(token, False))
    return styles


def apply_presentation_map(
    draft: bytes,
    styles: Mapping[str, PresentationStyle],
    /,
    *,
    source_snapshot: SnapshotRef,
    source_path: RepoPath,
) -> bytes:
    """Apply optional presentation hints; empty styles leave the draft unchanged."""
    if type(draft) is not bytes:
        raise TypeError("draft must be exact bytes")
    if not styles:
        return draft
    plan = build_markdown_plan(source_snapshot, source_path, draft)
    replacements: list[tuple[int, int, bytes]] = []
    for field in fields_of(plan):
        for region in field.protected_regions:
            if region.kind is not ProtectedKind.IDENTIFIER:
                continue
            start, end = region.span.start, region.span.end
            fragment = draft[start:end]
            token = _canonical_identifier(fragment)
            if token is None:
                continue
            style = styles.get(token)
            canonical = token.encode("utf-8")
            if style is not None and style.inline_code:
                replacements.append((start, end, b"`" + canonical + b"`"))
            elif fragment != canonical:
                replacements.append((start, end, canonical))
    if replacements:
        result = bytearray(draft)
        for start, end, replacement in sorted(
            replacements, key=lambda item: item[0], reverse=True
        ):
            result[start:end] = replacement
        return bytes(result)

    # Fallback for drafts that still contain escaped identifier prose.
    text = draft
    for token, style in styles.items():
        escaped = token.replace("_", "\\_").encode("utf-8")
        bare = token.encode("utf-8")
        if escaped in text:
            replacement = b"`" + bare + b"`" if style.inline_code else bare
            text = text.replace(escaped, replacement)
        elif style.inline_code:
            pattern = re.compile(rb"(?<!`)" + re.escape(bare) + rb"(?!`)")
            text = pattern.sub(b"`" + bare + b"`", text)
    return text
