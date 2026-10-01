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
residuals 1–9 закрыты против `REQUIREMENTS_RU.md`: soft-publish diagnostics
(Markdown/YAML/structure) не gate, continue для missing/null targets и
soft-published RED, zero-text NON_FINAL → RED, TOC same-name prune + empty-target
ancestor string IDs, линейный source-echo detector. См. `debt-map.md`.
Continue tests that still expected per-file `complete_pair` exclusion or
translation-PR continue after branch-delete/`target_sha=null` were rewritten to
§1.1/§1.2/§5.1 (stale expectations). Остаётся live `doc_translate` gate.
