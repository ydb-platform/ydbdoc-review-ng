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

Срезы 1–10 закрыты на local `main`. Offline non-live suite ранее: **2222 passed**.

Neural review triage (2026-10-01): P0 + часть P1 закрыты на `main`
(`55a95cb`, `865a63e`, `f35522b`, + publication/reporting). См. `debt-map.md`
таблицу findings 1–25. Live `doc_translate` gate и оставшиеся P1 (modes/TOC/
continue/echo) ещё открыты.

