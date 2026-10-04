"""Thin whole-file Markdown translation (chat-style DeepSeek loop).

Send the full source Markdown, receive the full target Markdown. No segment
JSON map, no opaque placeholders. Python publication gates decide whether the
result may ship.
"""

from __future__ import annotations

_THIN_SYSTEM = """You translate YDB Diplodoc/YFM documentation.
Return ONLY the full translated Markdown file. No preamble, no explanation,
no markdown fence wrapped around the entire file.
Rules:
- Preserve YFM/Diplodoc markup, {% include %}, {% note %}, anchors {#...},
  links, and templates like {{ ydb-short-name }} / {{ ydb-ui-name }}.
- Preserve code fences, shell commands, paths, identifiers, flags, and
  JSON/YAML keys exactly.
- Translate prose to natural technical English (or Russian when the target
  locale is ru).
- Never emit split-backtick underscore mangling such as `foo`_`bar`,
  word`_`word, or `_`path.
- Keep one logical paragraph on one physical Markdown line (no hard-wrap).
"""


def build_thin_translate_prompt(
    source_text: str,
    /,
    *,
    source_path: str,
    source_locale: str,
    target_locale: str,
    existing_target: str | None = None,
) -> str:
    """Build the user prompt for one thin whole-file translation call."""
    if type(source_text) is not str:
        raise TypeError("source_text must be str")
    direction = f"{source_locale}→{target_locale}"
    parts = [
        f"Translate this YDB documentation file from {direction}.",
        f"Source path: {source_path}",
        "Return the complete target Markdown only.",
    ]
    if existing_target:
        parts.append(
            "An older target file exists. Prefer updating it to match the source "
            "meaning and structure; do not leave source-locale prose in the result."
        )
        parts.append("=== OLD TARGET (reference) ===")
        parts.append(existing_target)
        parts.append("=== END OLD TARGET ===")
    parts.append("=== SOURCE ===")
    parts.append(source_text)
    parts.append("=== END SOURCE ===")
    return "\n".join(parts)


def thin_developer_prompt() -> str:
    return _THIN_SYSTEM


def unwrap_thin_response(text: str, /) -> str:
    """Strip accidental whole-file fences from a model response."""
    if type(text) is not str:
        raise TypeError("text must be str")
    body = text.strip("\n")
    if body.startswith("```"):
        lines = body.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        body = "\n".join(lines)
    if body and not body.endswith("\n"):
        body += "\n"
    return body
