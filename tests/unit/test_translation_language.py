from __future__ import annotations

import pytest

from ydbdoc_review_ng.translation import DocumentChunk, DocumentTranslationError
from ydbdoc_review_ng.translation.language import validate_translated_prose

PHRASE = "Этот абзац описывает выполнение запросов в распределённой базе данных"


@pytest.mark.parametrize("response", [
    "Translated introduction.\n\n" + PHRASE,
    "Translated introduction. " + PHRASE.replace(" ", "\n"),
])
def test_partial_source_echo_is_rejected_even_with_changed_whitespace(response: str) -> None:
    chunk = DocumentChunk("Введение.\n\n" + PHRASE, 0, 1, ())
    with pytest.raises(DocumentTranslationError, match="untranslated_source_prose"):
        validate_translated_prose(chunk, response, "ru", "en")


def test_short_russian_names_are_not_rejected() -> None:
    chunk = DocumentChunk(PHRASE + ". Яндекс.", 0, 1, ())
    validate_translated_prose(chunk, "Query execution in a distributed database. Яндекс.", "ru", "en")


def test_long_russian_prefix_inside_partially_translated_line_is_echo() -> None:
    """REQUIREMENTS §2.2: ≥32 Cyrillic letters as a fragment, not only the full run."""
    source = (
        "Достаточно длинный русский фрагмент остаётся нетронутым и весь дальнейший "
        "русский текст тоже является исходным."
    )
    response = (
        "Достаточно длинный русский фрагмент остаётся нетронутым and the rest is translated."
    )
    chunk = DocumentChunk(source, 0, 1, ())
    with pytest.raises(DocumentTranslationError, match="untranslated_source_prose"):
        validate_translated_prose(chunk, response, "ru", "en")


def test_russian_prose_is_valid_for_russian_target() -> None:
    chunk = DocumentChunk(PHRASE, 0, 1, ())
    validate_translated_prose(chunk, PHRASE, "en", "ru")


def test_noncontinuous_russian_fragments_are_not_joined_as_echo() -> None:
    """REQUIREMENTS §2.2: only continuous ≥32-letter runs count (#9)."""
    source = "Достаточно длинный русский фрагмент остаётся нетронутым"
    response = (
        "Достаточно длинный translated русский фрагмент translated остаётся нетронутым"
    )
    validate_translated_prose(DocumentChunk(source, 0, 1, ()), response, "ru", "en")


def test_source_echo_detector_is_sub_quadratic_on_long_clean_english() -> None:
    """REQUIREMENTS §2.2: detector must stay usable on whole-file paragraphs (#9)."""
    import time

    source = ("Большая русская статья объясняет работу базы данных. " * 40)[:960]
    started = time.perf_counter()
    validate_translated_prose(
        DocumentChunk(source, 0, 1, ()),
        "The English translation has no Russian text at all.",
        "ru",
        "en",
    )
    assert time.perf_counter() - started < 0.25
