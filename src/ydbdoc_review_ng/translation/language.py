"""Conservative detection of copied Russian prose in English translation chunks."""
from __future__ import annotations

import re

from ydbdoc_review_ng.translation.document import DocumentChunk, DocumentTranslationError

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_RUSSIAN_RUN = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё\s,;:.!?«»()—–-]*[А-Яа-яЁё]")


def _cyrillic_count(text: str) -> int:
    return len(_CYRILLIC.findall(text))


def _has_echo_fragment(phrase: str, normalized: str) -> bool:
    """§2.2: any continuous ≥32 Cyrillic-letter fragment of a source run counts.

    Continuity is over the space-normalized source text itself. Cyrillic letters
    separated by Latin must not be rejoined. Complexity is linear in phrase length
    times response scan cost for candidate windows (not cubic over all substrings).
    """
    compact = " ".join(phrase.split())
    if _cyrillic_count(compact) < 32:
        return False
    if compact in normalized:
        return True
    n = len(compact)
    prefix = [0] * (n + 1)
    for index, char in enumerate(compact):
        prefix[index + 1] = prefix[index] + (1 if _CYRILLIC.match(char) else 0)
    # Minimal contiguous window from each start that reaches ≥32 Cyrillic letters.
    end = 0
    for start in range(n):
        if prefix[n] - prefix[start] < 32:
            break
        if end < start:
            end = start
        while end <= n and prefix[end] - prefix[start] < 32:
            end += 1
        if end > n:
            break
        if compact[start:end] in normalized:
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
