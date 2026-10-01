"""Conservative detection of copied Russian prose in English translation chunks."""
from __future__ import annotations

import re

from ydbdoc_review_ng.translation.document import DocumentChunk, DocumentTranslationError

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_RUSSIAN_RUN = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё\s,;:.!?«»()—–-]*[А-Яа-яЁё]")


def _cyrillic_count(text: str) -> int:
    return len(_CYRILLIC.findall(text))


def _has_echo_fragment(phrase: str, normalized: str) -> bool:
    """§2.2: any continuous ≥32 Cyrillic-letter fragment of a source run counts."""
    if phrase in normalized:
        return True
    letters = _CYRILLIC.findall(phrase)
    if len(letters) < 32:
        return False
    # A long Cyrillic-letter window from the source run appears in the response.
    response_letters = "".join(_CYRILLIC.findall(normalized))
    cyr_only = "".join(letters)
    for start in range(0, len(cyr_only) - 31):
        if cyr_only[start : start + 32] in response_letters:
            return True
    # Spaced phrase prefixes/suffixes that still clear the letter threshold.
    compact = " ".join(phrase.split())
    for length in range(len(compact), 31, -1):
        for start in range(0, len(compact) - length + 1):
            fragment = compact[start : start + length]
            if _cyrillic_count(fragment) >= 32 and fragment in normalized:
                return True
    return False


def validate_translated_prose(
    chunk: DocumentChunk, response: str, source_locale: str, target_locale: str,
) -> None:
    """Check model-visible prose only; code/URLs/templates are already placeholders.

    This is deliberately not a general language detector. Long verbatim Russian
    spans catch partial source echoes while short names and quoted terms remain
    the semantic critic's responsibility.
    """
    if (source_locale, target_locale) != ("ru", "en"):
        return
    normalized = " ".join(response.split())
    for paragraph in re.split(r"\n[ \t]*\n", chunk.text):
        for match in _RUSSIAN_RUN.finditer(" ".join(paragraph.split())):
            phrase = match.group()
            if _cyrillic_count(phrase) >= 32 and _has_echo_fragment(phrase, normalized):
                raise DocumentTranslationError("document_response:untranslated_source_prose")
