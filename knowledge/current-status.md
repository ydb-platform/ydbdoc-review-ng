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

> [!success] Local BlobDepot critic+arbiter → YELLOW (mergeable); CI RED explained
> Run [37100488317](https://github.com/ydb-platform/ydb/actions/runs/37100488317) /
> [#54924](https://github.com/ydb-platform/ydb/pull/54924): «сбой модели» = our
> `TURN_BUDGET` (12) mislabeled as provider. Local grant iteration
> (`scripts/probe_blobdepot_critic_live.py`): turns=32 → critic finishes →
> arbiter YELLOW → no-op findings filtered to GREEN. Research:
> `knowledge/blobdepot-critic-research.md`. Residual prose (`Blob depot`,
> BS_CONTROLLER word order) still editorial, not budget.

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
- Stale [#54888](https://github.com/ydb-platform/ydb/pull/54888) closed; branch
  `translation/pr-50839` deleted.
- New open translation: [#54924](https://github.com/ydb-platform/ydb/pull/54924)
- Workflow: [37100488317](https://github.com/ydb-platform/ydb/actions/runs/37100488317)
  (`doc_translate` label on #50839, action `@v1.0.1` = tip `164f3e6`)

## Live clean re-run history

- 2026-10-02: tip then produced #54877 → … → **#54888** (one-shot critic era).
- 2026-10-03: P2 tool-critic live PASS; clean re-run → **#54924** (workflow in progress).

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
