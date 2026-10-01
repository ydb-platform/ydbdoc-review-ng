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

**READY** vs `REQUIREMENTS_RU.md` на offline contract surface.

Clean-slate review `78e0e58` Important 1–18 / Minor 19–22 закрыты ранее.
Tip residuals after `6c373d6` (four holes under claimed #6/#7/#17/#18) FIXED-NOW:
missing verify TOC soft-pending, critic TOC YAML soft-publish, delete-only
`resource-review` continue/persistence, plan-level source TOC delete. См. `debt-map.md`.

Focused regression: **291 passed** (tip-bug + plan/review/continue/persistence).
Full non-live suite after tip residuals: **2226 passed / 0 failed** (1 deselected).
One follow-up commit aligned stale e2e continue expectations (`54ce446`).

Live `doc_translate` gate ещё не доказан end-to-end credentials/push.
