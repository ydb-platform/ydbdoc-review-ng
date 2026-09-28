"""Conservative checks for copied source prose and collapsed translation aliases."""
from __future__ import annotations

import re
from collections import Counter

from ydbdoc_review_ng.translation.document import DocumentChunk, DocumentTranslationError

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_RUSSIAN_RUN = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё\s,;:.!?«»()—–-]*[А-Яа-яЁё]")
_STRONG = re.compile(r"(?<!\*)\*\*([^*\n]+?)\*\*(?!\*)")


def _strong_terms(paragraph: str, /) -> Counter[str]:
    return Counter(" ".join(match.split()).casefold() for match in _STRONG.findall(paragraph))


def validate_translated_prose(
    chunk: DocumentChunk, response: str, source_locale: str, target_locale: str,
) -> None:
    """Check model-visible prose only; code/URLs/templates are already placeholders.

    This is deliberately not a general language detector. Long verbatim Russian
    spans catch partial source echoes. Exact repeated bold terms catch aliases
    collapsed by translation; other terminology remains the critic's responsibility.
    """
    source_paragraphs = re.split(r"\n[ \t]*\n", chunk.text)
    source_maximum: Counter[str] = Counter()
    for paragraph in source_paragraphs:
        source_maximum |= _strong_terms(paragraph)
    for paragraph in re.split(r"\n[ \t]*\n", response):
        for term, count in _strong_terms(paragraph).items():
            if count > 1 and count > source_maximum[term]:
                raise DocumentTranslationError(
                    "document_response:duplicate_emphasized_alias"
                )
    if (source_locale, target_locale) != ("ru", "en"):
        return
    normalized = " ".join(response.split())
    for paragraph in re.split(r"\n[ \t]*\n", chunk.text):
        for match in _RUSSIAN_RUN.finditer(" ".join(paragraph.split())):
            phrase = match.group()
            if len(_CYRILLIC.findall(phrase)) >= 32 and phrase in normalized:
                raise DocumentTranslationError("document_response:untranslated_source_prose")
