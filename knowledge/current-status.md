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

Срезы 1–10 закрыты на `main`. Second external review residuals 1–12 (ba0acf2)
закрыты: NON_FINAL→RED, soft structure publish, null-checkpoint continue, resource
locale pairing, binary dependency manifest, TOC group-delete/ancestor/uncovered,
direction-continue TOC strings, GitHub 204, source-echo fragments, label mutation
terminal. См. `debt-map.md`. Остаётся live `doc_translate` gate.
