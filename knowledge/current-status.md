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
Tip A/B/C + prior residuals closed; lifecycle holes on `56d6bff` closed:

| Hole | Status |
|---|---|
| A Critic/verify TOC soft-publish (`RuntimeBoundaryError`) | **FIXED** |
| B Intentional TOC DELETE forced RED via required null | **FIXED** |
| C GREEN continue noop left stale RED QA | **FIXED** |
| 1 `doc_verify` already-absent intentional TOC delete → false RED | **FIXED** |
| 2 Multi-continue stale source-only RED QA next to translation link | **FIXED** |
| L1 Repeated verify RED → ambiguous continue | **FIXED** |
| L2 Stale unmarked «Актуальный» source link after next translate | **FIXED** |
| L3 `doc_verify` leaves leftover source RED when translation PR exists | **FIXED** |

Witnesses: `tests/integration/test_tip_abc_holes_cbd0632.py`,
`tests/integration/test_tip_residual_verify_source_qa.py`,
`tests/integration/test_tip_lifecycle_bugs_56d6bff.py`. См. `debt-map.md`.

Focused regression: tip lifecycle + residuals + continue/checkpoint suite.
Live `doc_translate` gate ещё не доказан end-to-end credentials/push.
