"""Small, source-relevant bilingual glossary excerpts for model prompts."""

from __future__ import annotations

import re

_HEADING = re.compile(r"(?m)^(#{2,6})\s+.*?\{#([A-Za-z0-9_.:-]+)\}\s*$")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _sections(markdown: str) -> dict[str, str]:
    matches = list(_HEADING.finditer(markdown))
    return {
        match.group(2): markdown[match.start() : matches[index + 1].start()].strip()
        for index, match in enumerate(matches)
        if index + 1 < len(matches)
    } | ({matches[-1].group(2): markdown[matches[-1].start() :].strip()} if matches else {})


def _stemmed_words(value: str) -> set[str]:
    return {word.casefold()[:6] for word in _WORD.findall(value) if len(word) >= 4}


def bilingual_glossary_context(
    source_text: str,
    source_glossary: bytes | None,
    target_glossary: bytes | None,
    /,
) -> str | None:
    """Select paired glossary sections whose declared terms occur in source prose."""
    if source_glossary is None or target_glossary is None:
        return None
    try:
        source_sections = _sections(source_glossary.decode("utf-8"))
        target_sections = _sections(target_glossary.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    document_words = _stemmed_words(source_text)
    selected: list[str] = []
    for anchor in sorted(source_sections.keys() & target_sections.keys()):
        section = source_sections[anchor]
        aliases = _BOLD.findall(section)
        term_sets = tuple(words for alias in aliases if (words := _stemmed_words(alias)))
        if not any(words <= document_words for words in term_sets):
            continue
        selected.append(
            f"<glossary-entry anchor=\"{anchor}\">\n"
            f"SOURCE:\n{section}\nTARGET:\n{target_sections[anchor]}\n"
            "</glossary-entry>"
        )
    return "\n\n".join(selected) or None
