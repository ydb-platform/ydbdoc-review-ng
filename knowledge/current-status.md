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

Срезы 1–10 закрыты на `main`. Second-review residuals 1–12 и third-review
residuals 1–9 закрыты против `REQUIREMENTS_RU.md`. Пять production bugs после
`bbb7495` закрыты: critic malformed YAML soft-publish, continue после soft YAML,
TOC-only/resource-only continue без Markdown scope, mixed MD+binary restore без
UTF-8 decode ассетов. См. `debt-map.md`. Continue tests с устаревшими
ожиданиями `complete_pair`/null-head переписаны на §1.1/§1.2/§5.1. Остаётся
live `doc_translate` gate.
