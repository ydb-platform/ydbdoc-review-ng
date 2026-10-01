# Текущее состояние

## Согласованный контракт (2026-10-01)

Упрощённый semantic flow в `REQUIREMENTS_RU.md`:

1. Файлы PR + дотянутые missing-target зависимости.
2. Direction: только «нужен перевод?» + направление (Python владеет Git-ops).
3. Whole-file translate; TOC — Python delta + перевод строк.
4. Всегда публикуем собранный UTF-8.
5. Critic правит полные файлы и пушит.
6. Arbiter: GREEN / YELLOW / RED.
7. YELLOW = успех (ручная правка + `doc_verify`); RED = continue / ручная правка.
8. Режимы: `doc_translate`, `doc_verify`, `doc_continue`.

## Код

Срезы 1–10 закрыты на local `main`. Offline non-live suite: **2222 passed**,
3 deselected (`live`). Continue_review / soft-publish / critic-push expectations
выровнены. Live `doc_translate` gate ещё не пройден (push 403 / credentials).
См. `debt-map.md` срез 11.
