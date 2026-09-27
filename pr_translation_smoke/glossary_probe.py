from __future__ import annotations

import re
from dataclasses import dataclass


_OPAQUE_TOKEN = re.compile(r"\[\[OPAQUE_\d{4}\]\]")
_OPAQUE_SCAN = re.compile(
    r"(?ms)^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^\s*(?P=fence)[ \t]*$"
    r"|(?P<inline>`+[^`\n]+`+)"
    r"|(?P<yfm>\{\{[^\n{}]+\}\}|\{%.*?%\})"
    r"|(?P<autolink><https?://[^>\s]+>)"
    r"|\]\((?P<destination>[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)"
)


@dataclass(frozen=True)
class OpaqueFragment:
    token: str
    source: str


def mask_opaque_fragments(source: str) -> tuple[str, tuple[OpaqueFragment, ...]]:
    """Mask only source-owned syntax that a translator must not rewrite.

    Markdown emphasis and block/list structure intentionally remain visible to the
    model. Link labels remain visible; only link destinations are opaque.
    """

    fragments: list[OpaqueFragment] = []

    def replace_clean(match: re.Match[str]) -> str:
        value = match.group("destination") if match.group("destination") is not None else match.group(0)
        token = f"[[OPAQUE_{len(fragments):04d}]]"
        fragments.append(OpaqueFragment(token, value))
        if match.group("destination") is not None:
            return "](" + token + ")"
        return token

    return _OPAQUE_SCAN.sub(replace_clean, source), tuple(fragments)


def restore_opaque_fragments(
    translated: str, fragments: tuple[OpaqueFragment, ...]
) -> str:
    expected = [item.token for item in fragments]
    actual = _OPAQUE_TOKEN.findall(translated)
    if actual != expected:
        raise ValueError(
            "opaque token contract violation: "
            f"expected {expected}, got {actual}"
        )
    values = {item.token: item.source for item in fragments}
    return _OPAQUE_TOKEN.sub(lambda match: values[match.group(0)], translated)


def build_glossary_prompt(
    *, source_path: str, source_locale: str, target_locale: str, masked_markdown: str
) -> str:
    return (
        "Translate the complete Markdown document below from "
        f"{source_locale} to {target_locale}.\n"
        "Return only the complete translated Markdown. Do not summarize, omit, "
        "reorder, or add sections. Keep Markdown structure visible and preserve "
        "every [[OPAQUE_NNNN]] token exactly once and in its original order.\n"
        f"Source path: {source_path}\n\n"
        "<DOCUMENT>\n"
        f"{masked_markdown}"
        "\n</DOCUMENT>"
    )


def split_markdown_chunks(markdown: str, *, max_chars: int = 12_000) -> tuple[str, ...]:
    """Split only at newline boundaries and preserve the input byte-for-byte."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    lines = markdown.splitlines(keepends=True)
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    fence: str | None = None
    for line in lines:
        stripped = line.lstrip()
        if current and current_chars + len(line) > max_chars and fence is None:
            chunks.append("".join(current))
            current = []
            current_chars = 0
        current.append(line)
        current_chars += len(line)
        marker = stripped[:3] if stripped.startswith(("```", "~~~")) else None
        if marker is not None:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
    if current:
        chunks.append("".join(current))
    return tuple(chunks)
