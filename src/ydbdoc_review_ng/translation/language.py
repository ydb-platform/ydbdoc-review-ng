"""Conservative detection of copied Russian prose in English translation chunks."""
from __future__ import annotations

import re

from ydbdoc_review_ng.translation.document import DocumentChunk, DocumentTranslationError

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_RUSSIAN_RUN = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё\s,;:.!?«»()—–-]*[А-Яа-яЁё]")


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
            if len(_CYRILLIC.findall(phrase)) >= 32 and phrase in normalized:
                raise DocumentTranslationError("document_response:untranslated_source_prose")
