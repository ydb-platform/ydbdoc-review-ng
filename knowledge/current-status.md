# Текущее состояние

## Согласованный контракт (2026-10-02 redesign)

Semantic flow в `REQUIREMENTS_RU.md`:

1. Файлы PR + дотянутые missing-target зависимости.
2. Direction: только «нужен перевод?» + направление (Python владеет Git-ops).
3. Prep: placeholders + **identifier atoms** (underscore + CamelCase product) +
   presentation map from old target (atoms, CLI flags, short ALLCAPS, colon-form).
4. Whole-file translate (old target = presentation reference only when present) → **draft**.
5. Critic = обязательный gate → **reviewed** commits; fail/503/unreviewed → RED.
6. Arbiter только на reviewed bytes: GREEN / YELLOW / RED.
7. YELLOW = успех; RED = continue / ручная правка + `doc_verify`.
8. Soft-publish diagnostics ≠ reader-facing product success.
9. Режимы: `doc_translate`, `doc_verify`, `doc_continue`.

## Код (P0+P1+P2 + quality hotfix 2026-10-02)

| Item | Status |
|---|---|
| Identifier atoms (`BS\_CONTROLLER` not split) | **DONE** |
| CamelCase product atoms (`BlobDepot`) | **DONE** |
| Presentation map: atoms + CLI flags + ALLCAPS + `gen:counter` | **DONE** |
| Draft vs reviewed critic gate (fail → RED) | **DONE** |
| Model HTTP timeout default 600s (+ env override) | **DONE** |
| Translator/critic/arbiter prompt updates | **DONE** |
| Canon §1.2 / §2 / §4.1 / §5.1 / §7 | **DONE** |
| P2 BlobDepot golden harness | **DONE** |

## Critic fall (run 37009373894) — root cause

Live PR [#54842](https://github.com/ydb-platform/ydb/pull/54842) commit `4f39103`
(translator-only). Critic 2× `TRANSPORT`, `http_status=null`, wall ~182s.
Root cause: `_BaseYandexClient` default `timeout_seconds=180` vs
`reasoning_effort=high` for critic. urllib timeout → transport failure → RED,
raw draft shipped. Fix: default **600s**, env
`YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS`, keep `reasoning_effort=high` (canon §4).

## Quality classes on #54842 raw EN (critic never ran)

| Class | Root cause | Fixed in code? |
|---|---|---|
| BlobDepot → «Blob depot» | CamelCase not an atom; glossary #54797 **unmerged** so prompt path empty (wiring OK when entry present) | **Yes** — CamelCase atoms; glossary select regression |
| Lost backticks on `--name`, `NEW`, `WORKING` | Presentation map required `_`/`::` | **Yes** — map covers CLI flags + ALLCAPS |
| `gen:counter` lost backticks / escapes | Colon-form not in map | **Yes** |
| «command is executed BS_CONTROLLER» | Model prose around opaque atom | **No** — needs critic (timeout unblock) |
| CREATED_FAILED typo | RU source atom `CREATED\_FAILED` preserved | **No** — model/critic; source typo |
| `{{ ydb-name }}` word order | Template restore + RU order | **Mostly model**; template bytes intact |

> [!warning] Do not start another live run
> Code fix first. User did not say «гони».

**TOC serialization fix** still at prior tip; this hotfix lands on top.
Pin `v1.0.1` moves with this commit after push.
