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

Clean-slate review `78e0e58` Important 1–18 / Minor 19–22 закрыты (21 FIXED-NOW,
1 INCORRECT = #2 mixed-locale). См. `debt-map.md`.

Offline suite: **0 failed / 2137 passed** (1 deselected). Residual 14 failures after
clean-slate were stale expectations vs soft-publish / §4.1 zero-text critic /
label DELETE — tests updated, no production regression found.

Live `doc_translate` gate ещё не доказан end-to-end credentials/push.
