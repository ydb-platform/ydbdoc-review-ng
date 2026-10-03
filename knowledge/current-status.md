# Текущее состояние

## Согласованный контракт (2026-10-02 redesign + 2026-10-03 critic tools)

Semantic flow в `REQUIREMENTS_RU.md`:

1. Файлы PR + дотянутые missing-target зависимости.
2. Direction: только «нужен перевод?» + направление (Python владеет Git-ops).
3. Prep: placeholders + **identifier atoms** (underscore + CamelCase product) +
   presentation map from old target (atoms, CLI flags, short ALLCAPS, colon-form).
4. Whole-file translate (old target = presentation reference only when present) → **draft**.
5. Critic = обязательный tool-using gate (§4.1): workspace + read/grep/apply_patch
   + mandatory re-read → **reviewed** commits; fail → RED.
6. Arbiter только на reviewed bytes: GREEN / YELLOW / RED (judge-only, no repair).
7. YELLOW = успех; RED = continue / ручная правка + `doc_verify`.
8. Soft-publish diagnostics ≠ reader-facing product success.
9. Режимы: `doc_translate`, `doc_verify`, `doc_continue`.

## Код vs новый critic contract (2026-10-03)

| Item | Status |
|---|---|
| Identifier atoms / presentation map / draft-reviewed gate | **DONE** (prior) |
| P2 BlobDepot golden harness (one-shot critic era) | **DONE** (prior) |
| REQUIREMENTS §4.1 tool-using critic | **DONE (docs)** + adversarial fixups |
| Plan `knowledge/tool-using-critic-plan.md` | **DONE (docs)** + adversarial fixups |
| Runtime tool loop / client `tool_calls` | **NOT STARTED** (P1 next) |
| P0 live DeepSeek tools capability probe | **PASS / GO** — `knowledge/p0-deepseek-tools-probe.md` |
| Offline stub-tool integration tests | **NOT STARTED** |

> [!important] P0 green; P1 implementation unblocked
> Live probe (2026-10-03): DeepSeek `deepseek-v4-flash` on
> `ai.api.cloud.yandex.net` returns `finish_reason=tool_calls` with
> `content=null` and accepts `role=tool` multi-turn. Shell used
> `YANDEX_CLOUD_*_DOC_REVIEW` (production `YANDEX_*` absent locally).
> Tip still runs one-shot JSON critic until P1 cutover. #54888 re-run
> only after P2 + Actions `YANDEX_*`.

## Critic TRANSPORT (runs 37009373894 → 37027975808) — real root cause

Not «provider flaky». Translator OK; critic 2× `TRANSPORT`, `http_status=null`.

| Run | tip | wall per critic attempt | limiter |
|---|---|---|---|
| 37009373894 | pre-timeout bump | ~182s | **our** urllib 180s |
| 37027975808 | `4f2867c` (600s) | ~273s | **provider** silent wall |

Fix on tip: one pair/chunk, relevant glossary only, cap `max_output_tokens`.
Tool-using critic keeps one-pair chunking; patches shrink generation further.

## Quality classes on #54842 / #54888 lineage

Manual review of [#54888](https://github.com/ydb-platform/ydb/pull/54888)
(source [#50839](https://github.com/ydb-platform/ydb/pull/50839) BlobDepot):
structure/links/images largely OK; residual prose/literals (`--wait wait`,
awkward phrasing, inconsistent inline-code). Whole-file critic did not reliably
apply fixes. Tool-using critic is the agreed remedy (not arbiter↔repair loops).

## Latest open BlobDepot translation PR (confirmed 2026-10-03)

- Source: [#50839](https://github.com/ydb-platform/ydb/pull/50839)
- Open translation: [#54888](https://github.com/ydb-platform/ydb/pull/54888)
  (`translation/pr-50839`)
- Closed predecessors include #54886, #54877, #54868, …

## Live clean re-run (2026-10-02, user «гони»)

- Tip then: `v1.0.1` = `a229b39` lineage; later tips `45712c3` / `28e2da1`.
- Workflows produced #54877 → … → **#54888** (open).
- Next live `doc_translate` only after tool-critic implementation + tag (P2).

## YC / live model env (names found vs missing)

**Found:** `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`, `YDBDOC_MODEL*`,
`YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS`, `YDBDOC_DAILY_BUDGET_RUB`, `YDBDOC_LIVE`,
smoke aliases `YC_API_KEY` / `YDBDOC_YC_API_KEY` / `YC_FOLDER_ID` /
`YDBDOC_MODEL_TRANSLATE`, hardcoded `OPENAI_ENDPOINT` / `NATIVE_ENDPOINT`.

**Missing for grant-limited paid tests:** grant id / remaining quota env names;
unified live creds (prod `YANDEX_*` vs smoke `YC_*`). DeepSeek tool_calls
proof: **done** (`knowledge/p0-deepseek-tools-probe.md`). Production tools
feature-flag dual-path **rejected** (rollback = tip revert). Details:
`knowledge/tool-using-critic-plan.md`.
