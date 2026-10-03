# Plan: tool-using critic (reliable rollout)

Status: **plan + requirements only** (2026-10-03). Runtime tool loop **not**
implemented yet. Independent review of this plan is the next gate before code.

Canon after this commit: `REQUIREMENTS_RU.md` §4.1 (tool workspace + patches).
This file is the execution plan: phases, TDD list, risks, acceptance, delivery.

## Problem

Live BlobDepot lineage (source [#50839](https://github.com/ydb-platform/ydb/pull/50839),
latest open translation [#54888](https://github.com/ydb-platform/ydb/pull/54888))
shows the one-shot whole-file critic can name defects and still leave wrong
bytes in the PR (`--wait wait`, awkward phrasing, inconsistent inline-code).
Returning complete files in JSON burns tokens, hits silent-connection walls,
and does not force the model to verify its own edits.

Rejected design: closed-loop arbiter findings → repair → re-arbitrate N times
(token burn, arbiter stays judge-only by contract).

Chosen design: **one tool-using critic session per chunk** that edits a
workspace via patches, **must re-read touched lines**, then runtime publishes
the workspace bytes as reviewed. Arbiter remains a single judge pass.

## Goals / non-goals

### Goals

1. Critic edits draft target through bounded tools (`read` / `grep` /
   `apply_patch`), not by dumping whole files as the primary response.
2. After every successful patch, critic **must** re-read the touched region;
   session fails closed if it skips re-read.
3. **Final published bytes** = runtime-applied patch accumulation on the
   draft workspace (not model-echoed full files, not arbiter text).
4. Arbiter stays judge-only on reviewed bytes. No arbiter↔repair loop.
5. Fail / budget / invalid tool use → RED / unreviewed like today.
6. Translator and TOC Python-delta path stay unchanged.

### Non-goals (this rollout)

- Multi-agent repair after arbiter.
- Changing direction / translator / soft-publish draft semantics.
- Replacing Diplodoc build gates with model verdicts.
- Implementing the tool loop in the same commit as this plan.

## Current code facts (must verify in P0)

| Fact | Evidence |
|---|---|
| OpenAI client sends `messages` + optional `response_format.json_schema` only | `src/ydbdoc_review_ng/models/clients.py` `YandexOpenAIClient._payload` |
| Native client has no tools either | `NativeYandexClient._payload` |
| Response parser expects `message.content` text only; ignores `tool_calls` | `_parse_openai` |
| `ModelRequest` has no tools / multi-turn fields | `models/types.py` |
| Critic builder: one `ModelRequest`, schema `{"files":…}` | `quality/critic.py` |
| Production roles use DeepSeek via OpenAI-compatible endpoint | `runtime.py` + `OPENAI_ENDPOINT` |
| `reasoning_effort` today: critic `medium`, arbiter `none` (tip `28e2da1`) | `clients.py` (REQUIREMENTS must stay aligned) |

**P0 gate:** prove DeepSeek V4 Flash on Yandex AI Studio OpenAI endpoint
actually returns OpenAI-style `tool_calls` (or document the exact supported
shape). If unsupported, stop and renegotiate (native tools? different model?
stay on JSON files with smaller patches-only schema). Do not invent a fake
tool protocol that the provider will not execute.

## Target architecture

```
draft bytes (soft-publish)
    → critic workspace (in-memory copy per chunk)
    → multi-turn DeepSeek session with tools
         read(path, start_line?, end_line?)
         grep(path|workspace, pattern, …)
         apply_patch(path, unified_diff|hunk)
         (optional later: list_paths — not required for v1)
    → after each apply_patch: runtime applies hunk; critic must call read
      on touched lines before next patch or finish
    → finish signal: assistant message with no tool calls + small JSON
      `{"status":"done"}` OR explicit `finish` tool (pick one in P0; prefer
      finish tool so schema stays strict)
    → reviewed commit = workspace bytes
    → arbiter once on reviewed bytes (unchanged contract)
```

### Hard reliability rules

1. **Bounded tool turns** per chunk (proposed default: 12; env override
   `YDBDOC_CRITIC_MAX_TOOL_TURNS` documented in P1). Exceed → unreviewed RED.
2. **Patch-not-full-file:** `apply_patch` rejects hunks that replace ≥ N% of
   file or exceed byte cap (proposed: reject if patched span > 8 KiB or >
   40% of file unless file < 2 KiB). Force surgical edits.
3. **Mandatory re-read:** after patch apply, next model action that is not
   `read` covering every touched line range → protocol error → retry once →
   RED.
4. **No token-burn arbiter loop:** arbiter findings never feed critic in the
   same job.
5. **Same fail→RED:** transport / NON_FINAL / invalid tool args / patch
   reject after retry → unreviewed paths RED; arbiter skipped for those paths.
6. **Chunking stays** one source/target pair (TOC pair separate); tools do not
   reopen whole-PR mega-context.
7. **TOC:** structural ownership remains Python §3. Critic may patch TOC text
   only through the same workspace rules; target-only entries protection from
   tip `28e2da1` remains a runtime validator (invalid patch → retry/RED /
   keep Python delta).

## Phases

### P0 — Provider + contract freeze (no production loop yet)

1. Independent review of this plan + `REQUIREMENTS_RU.md` §4.1.
2. Live **capability probe** (paid, gated): one chat with tools against
   DeepSeek V4 Flash on `https://ai.api.cloud.yandex.net/v1/chat/completions`
   using production-shaped auth (`YANDEX_API_KEY` + `YANDEX_FOLDER_ID` or
   documented smoke aliases). Record: HTTP status, whether `tool_calls`
   present, finish_reason, cost. Store result summary in
   `knowledge/current-status.md` (no secrets).
3. Freeze tool JSON schemas and finish signal in REQUIREMENTS (already
   drafted below in canon; adjust only if probe forces it).
4. Decide finish API: `finish` tool vs empty-tool final JSON. Prefer `finish`.

Exit: written probe result + go/no-go.

### P1 — TDD offline implementation (main)

Order is vertical slices; each slice: failing tests → minimal code → green
focused suite → commit `main`.

| Slice | Deliverable |
|---|---|
| P1a | Workspace + `apply_patch` / `read` / `grep` pure functions |
| P1b | Tool-loop driver with turn budget + mandatory re-read FSM |
| P1c | OpenAI client: send `tools`, parse `tool_calls`, multi-turn invoke API |
| P1d | Wire critic role to loop; drop whole-file JSON as primary path |
| P1e | Runtime: reviewed bytes from workspace; fail→RED unchanged |
| P1f | Prompts: critic as editor-with-tools; arbiter untouched |
| P1g | Knowledge + REQUIREMENTS sync if probe tweaked schemas |

Translator, direction, TOC Python delta: **no behavior change** except critic
input/output wiring.

### P2 — Integration + optional live + delivery

1. Offline integration: stub tool model that patches `--wait wait` → `--wait`,
   re-reads, finishes; assert published bytes and RED on skip-reread / too
   many turns.
2. Extend BlobDepot golden / sibling fixture for tool-critic path (still no
   network).
3. Optional live test `@pytest.mark.live` gated by env (see YC note).
4. Full non-live suite + Ruff + mypy + `git diff --check`.
5. Tag + push `public/main`.
6. Re-run translation on BlobDepot lineage: close/replace stale
   `translation/pr-50839` if needed, label `doc_translate` on #50839, expect
   new PR superseding [#54888](https://github.com/ydb-platform/ydb/pull/54888).
7. Independent content review of resulting PR (human/agent): GREEN/YELLOW
   target; residual RED must be real product issues, not leftover `--wait wait`.

## TDD test list

### Unit (always on, no network)

1. `apply_patch` applies unified hunk; rejects overlapping/invalid/oversized.
2. `read` returns exact line window with stable 1-based numbers.
3. `grep` returns path+line+snippet; respects workspace bytes not disk.
4. FSM: patch without subsequent covering `read` → protocol error.
5. FSM: turn counter hits max → stop with unreviewed failure.
6. FSM: `finish` with pending unre-read patches → error.
7. Client payload includes `tools` for critic role only; translator payload
   unchanged (no tools).
8. Client parses `tool_calls` + round-trips tool results as `role=tool`.
9. Critic success publishes workspace bytes, not assistant prose.
10. Critic fail/503/protocol → RED; arbiter not invoked on those paths
    (extend `test_draft_reviewed_gate`).
11. TOC target-only protection still rejects bad patches (adapt tip tests).
12. Patch that would delete target-only TOC entries invalid.

### Integration offline (stub model)

13. End-to-end chunk: stub issues grep → read → apply_patch → read → finish;
    translation branch gets patched file.
14. Stub skips re-read → RED + checkpoint semantics unchanged.
15. Stub exceeds turn budget → RED.
16. Stub tries full-file patch → rejected → retry → RED if persists.
17. Translator still whole-file JSON; TOC Python delta still applied before
    critic workspace seed.
18. Arbiter still judge-only; no second critic call from findings.

### Optional live (deselected by default)

19. `@pytest.mark.live` tool-call smoke: real DeepSeek, tiny fixture file,
    assert at least one `tool_calls` round and a successful patch+reread.
    Gate: `YDBDOC_LIVE=1` **and** production-shaped model creds (see below).
20. Do **not** auto-run live in CI; cost against grant/budget is manual.

## Yandex Cloud / live env vars (findings)

### Present in repo (names only)

| Name | Where | Role |
|---|---|---|
| `YANDEX_API_KEY` | Actions secrets, `runtime.py`, deployment.md | Production model API key |
| `YANDEX_FOLDER_ID` | Actions vars, runtime, deployment.md | Folder / OpenAI-Project |
| `YDBDOC_MODEL` | deployment.md | Default model (`deepseek-v4-flash`) |
| `YDBDOC_MODEL_CRITIC` / `YDBDOC_MODEL_ARBITER` / `YDBDOC_MODEL_FALLBACK` | deployment.md | Role overrides (docs may lag tip defaults) |
| `YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS` | deployment.md, runtime | urllib timeout (default 600) |
| `YDBDOC_DAILY_BUDGET_RUB` | deployment.md, translate CLI | Moscow-day spend gate for translate |
| `YDBDOC_LIVE` | README, smoke runners, glossary probe | Explicit opt-in for paid calls |
| `YC_API_KEY` / `YDBDOC_YC_API_KEY` | `pr_translation_smoke/`, README | Smoke/probe key aliases (**not** what `RecordedModels` reads) |
| `YC_FOLDER_ID` | smoke/README | Smoke folder alias |
| `YDBDOC_MODEL_TRANSLATE` | smoke/README | Smoke model uri |
| Hardcoded endpoints | `clients.py` | `NATIVE_ENDPOINT`, `OPENAI_ENDPOINT` |
| `YDB_ENDPOINT` / `YDB_DATABASE` / `YDB_TOKEN` / `YDB_SA_KEY` | deployment, live YDB tests | Persistence, not LLM |
| `YDBDOC_LIVE_YDB_REPOSITORY` | live diplodoc test | Local YDB checkout path |

### Missing for paid live tests against grant limits

| Gap | Impact |
|---|---|
| No env name for **grant id / billing account / promo grant** | Cannot document or assert “use the grant” in tests |
| No env for **grant remaining quota / hard stop besides** `YDBDOC_DAILY_BUDGET_RUB` | Budget gate is RUB/day in YDB audit, not YC grant balance |
| No unified live-test credential story | Production code wants `YANDEX_*`; smoke wants `YC_*` |
| No env for tool-calling feature flag | Needed for staged rollout (`YDBDOC_CRITIC_TOOLS=1` proposed in P1) |
| No checked-in proof DeepSeek tools work on YC | P0 probe must create it |
| Personal-shell names (`SINTJURI_*`) appear only in vault daily notes, **not** in this repo | Out of band; do not encode as project contract |

**Ready for live paid critic tests only after:** P0 probe green + operator
exports `YDBDOC_LIVE=1` with either production `YANDEX_API_KEY`+
`YANDEX_FOLDER_ID` (preferred for runtime path) or smoke aliases if the probe
script uses smoke clients + a numeric `YDBDOC_DAILY_BUDGET_RUB` / operator
spend discipline. Grant-limit automation is **not** available from repo docs.

## Risks

| Risk | Mitigation |
|---|---|
| DeepSeek on YC lacks tool_calls | P0 probe; fallback redesign before coding loop |
| Model loops patches without converging | Turn cap + patch size cap + RED |
| Model “finishes” without re-read | FSM hard-require |
| Token cost rises vs one-shot JSON | Smaller prompts (one pair) + patches; audit costs |
| TOC structural damage | Keep Python validator; invalid → retry/RED |
| Dual env names confuse live runs | Document matrix; prefer `YANDEX_*` for runtime tests |
| REQUIREMENTS/code drift on reasoning_effort | Sync in same docs commit; re-check in P1c |

## Acceptance

1. Offline suite green with stub tool model; new witnesses in
   `knowledge/testing.md` checked off.
2. Critic primary path is tools+patches; whole-file `{"files":…}` is not
   production primary (legacy parse may exist only if explicitly kept as
   emergency — default: **remove**).
3. Arbiter unchanged judge-only; no repair loop code paths.
4. Translator + TOC Python path unchanged by behavioral tests.
5. Fail→RED preserved.
6. Optional live tool smoke documented and skipped by default.
7. After tag: BlobDepot re-translate from #50839 produces a new PR; residual
   defects are triageable findings, not “critic said fix but bytes untouched”.

## Delivery checklist (end of P2)

1. `git pull --ff-only` `public/main`; work on `main`.
2. Implement P1/P2 with atomic commits.
3. Full non-live suite once.
4. Push `public` with `YDB_GH_TOKEN` and cleared `credential.helper` (same
   discipline as this docs commit).
5. Tag release tip (move or mint per delivery.md practice).
6. Close stale translation PR / delete `translation/pr-50839` if required.
7. Label `doc_translate` on source [#50839](https://github.com/ydb-platform/ydb/pull/50839).
8. Confirm new open translation PR (successor of #54888 lineage).
9. Independent review of plan **before** step 2; independent review of PR
   **after** step 8.

## Ready for independent review?

**Yes — for the plan and §4.1 contract.** Not ready for implementation review
until P0 DeepSeek tool probe is recorded. Not ready for live BlobDepot
acceptance until P2 delivery checklist completes.
