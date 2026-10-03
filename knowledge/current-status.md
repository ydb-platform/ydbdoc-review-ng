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
| Runtime tool loop / client `tool_calls` | **DONE** P1a–g offline + P2 live smoke |
| P0 live DeepSeek tools capability probe | **PASS / GO** — `knowledge/p0-deepseek-tools-probe.md` |
| Offline stub-tool integration tests | **PASS** (`pytest -m 'not live'`, 2026-10-03) |
| P2 live tool-critic smoke (`--wait wait`) | **PASS** (2026-10-03) — `tests/live/test_tool_critic_live.py` |

> [!success] P2 live PASS; BlobDepot clean re-run in progress
> Live smoke (2026-10-03): production-shaped `YANDEX_*` mapped from shell
> `YANDEX_CLOUD_*_DOC_REVIEW`; real DeepSeek tool loop patched tiny
> `--wait wait` fixture (~17s). Soft `ToolError` (e.g. `end past EOF`) now
> returns to the model instead of aborting the session. Tag `v1.0.1` moved
> with the fix; consumer `ydbdoc-review.yml` pins `@v1.0.1`.

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

## BlobDepot translation lineage (2026-10-03 P2)

- Source: [#50839](https://github.com/ydb-platform/ydb/pull/50839) (merged)
- Stale open translation [#54888](https://github.com/ydb-platform/ydb/pull/54888)
  (`translation/pr-50839`) closed/deleted for clean re-run after P2 PASS.
- Consumer trigger: label `doc_translate` on #50839 → workflow
  `ydbdoc-review (doc_translate label)` uses action `@v1.0.1`.

## Live clean re-run history

- 2026-10-02: tip then produced #54877 → … → **#54888** (one-shot critic era).
- 2026-10-03: P2 tool-critic live PASS; clean re-run from #50839 after closing #54888.

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
