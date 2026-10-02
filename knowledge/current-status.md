# Текущее состояние

## Согласованный контракт (2026-10-02 redesign)

Semantic flow в `REQUIREMENTS_RU.md`:

1. Файлы PR + дотянутые missing-target зависимости.
2. Direction: только «нужен перевод?» + направление (Python владеет Git-ops).
3. Prep: placeholders + **identifier atoms** + optional presentation map from old target.
4. Whole-file translate (old target = presentation reference only when present) → **draft**.
5. Critic = обязательный gate → **reviewed** commits; fail/503/unreviewed → RED.
6. Arbiter только на reviewed bytes: GREEN / YELLOW / RED.
7. YELLOW = успех; RED = continue / ручная правка + `doc_verify`.
8. Soft-publish diagnostics ≠ reader-facing product success.
9. Режимы: `doc_translate`, `doc_verify`, `doc_continue`.

## Код (P0+P1+P2 landed)

| Item | Status |
|---|---|
| Identifier atoms (`BS\_CONTROLLER` not split) | **DONE** |
| Presentation map apply / absent no-op | **DONE** |
| Draft vs reviewed critic gate (fail → RED) | **DONE** |
| Translator/critic/arbiter prompt updates | **DONE** |
| Canon §1.2 / §2 / §4.1 / §5.1 / §7 | **DONE** |
| Gate + atoms witnesses | **DONE** |
| P2 BlobDepot golden harness | **DONE** — `tests/golden/test_blobdepot_golden.py` |

Offline redesign surface (atoms + presentation map + draft/reviewed gate +
BlobDepot golden) is ready. Live `doc_translate` re-proof on a known source PR
is still pending (credentials + model quality on real #50839).
