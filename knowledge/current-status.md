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

Срезы 1–10, second/third-review residuals, five production continue/critic
bugs, и independent-audit findings A/B закрыты. См. `debt-map.md`.

Остаточный operational риск: live `doc_translate` gate ещё не доказан
end-to-end credentials/push.
