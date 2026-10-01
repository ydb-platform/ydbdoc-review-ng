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

Срезы 1–3, 5–8 закрыты на local `main` (dependency+limits, soft-publish,
critic chunking+immediate push, continuation v3, YELLOW=success, whole-file
translate, direction-only). TOC §3 пока append-only.
Остаётся: полный TOC delta, удаление мёртвого `select_direction`,
`doc_verify`/budget — см. `debt-map.md`.
