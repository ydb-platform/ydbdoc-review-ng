# Plan: tool-using critic (reliable rollout)

Status: **P2 LIVE PASS** (2026-10-03). Provider tool_calls proven
(`knowledge/p0-deepseek-tools-probe.md`); offline suite green; live
`--wait wait` smoke green (`tests/live/test_tool_critic_live.py` +
`scripts/probe_tool_critic_live.py`). Soft workspace `ToolError` returns to
the model (no session abort). BlobDepot clean re-run after close of #54888.
Independent adversarial review applied; contract holes below
are frozen in `REQUIREMENTS_RU.md` §0 / §3.6 / §4.1 before any code.

Canon: `REQUIREMENTS_RU.md` §4.1 (tool workspace + patches + enforceable FSM).
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
   `apply_patch` / `finish`), not by dumping whole files as the primary response.
2. After every successful patch, critic **must** re-read the touched region;
   session fails closed if it skips re-read (runtime FSM, not prompt hope).
3. **Final published bytes** = runtime-applied patch accumulation on the
   draft workspace (not model-echoed full files, not arbiter text).
4. Arbiter stays judge-only on reviewed bytes. No arbiter↔repair loop.
5. Fail / budget / invalid tool use → RED / unreviewed like today, with the
   sole TOC exception in §3.6 (Python-delta TOC → arbiter after two
   target-only protection failures).
6. Translator and TOC Python-delta path stay unchanged except critic wiring
   and that TOC exception.

### Non-goals (this rollout)

- Multi-agent repair after arbiter.
- Changing direction / translator / soft-publish draft semantics.
- Replacing Diplodoc build gates with model verdicts.
- Implementing the tool loop in the same commit as this plan.
- Production dual-path / feature-flag fallback to whole-file JSON.

## Current code facts (verified at tip `20dd21b`)

| Fact | Evidence |
|---|---|
| OpenAI client sends `messages` + optional `response_format.json_schema` only | `clients.py` `YandexOpenAIClient._payload` |
| Native client has no tools either | `NativeYandexClient._payload` |
| Response parser expects `message.content` text only; ignores `tool_calls` | `_parse_openai` |
| `finish_reason != "stop"` today → `NON_FINAL`; empty/null content → errors | `_semantic_error` — **landmine** for `finish_reason=tool_calls` |
| `ModelRequest` has no tools / multi-turn fields | `models/types.py` |
| Critic builder: one `ModelRequest`, schema `{"files":…}` | `quality/critic.py` |
| Production roles use DeepSeek via OpenAI-compatible endpoint | `runtime.py` + `OPENAI_ENDPOINT` |
| `reasoning_effort` today: critic `medium`, arbiter `none` | `clients.py` (keep REQUIREMENTS aligned) |

**P0 gate:** prove DeepSeek V4 Flash on Yandex AI Studio OpenAI endpoint
actually returns OpenAI-style `tool_calls` (or document the exact supported
shape). If unsupported, stop and renegotiate (native tools? different model?
stay on JSON files with smaller patches-only schema). Do not invent a fake
tool protocol that the provider will not execute.

**P0 result (2026-10-03): PASS / GO.** Live probe
`scripts/probe_deepseek_tools.py`: `finish_reason=tool_calls`, `content=null`,
`tool_calls[]` present; `role=tool` round-trip → `finish_reason=stop`. Details:
`knowledge/p0-deepseek-tools-probe.md`.

## Target architecture

```
draft bytes (soft-publish)
    → critic workspace (in-memory copy per chunk)
         writable: draft target (missing required target → 0-byte seed)
         read-only: source, relevant glossary, presentation-ref, TOC snapshots
    → multi-turn DeepSeek session with tools
         read / grep / apply_patch / finish
    → runtime FSM: pending_reread after each successful patch; covering read
      required before next mutate/finish
    → finish tool → reviewed commit = writable workspace bytes
    → arbiter once on reviewed bytes (unchanged judge-only contract)
```

### Hard reliability rules (canon detail in §4.1)

1. **Bounded tool turns** per chunk (default 12; `YDBDOC_CRITIC_MAX_TOOL_TURNS`).
2. **Patch-not-full-file** caps (plan P1 numbers: reject span > 8 KiB or > 40%
   unless file < 2 KiB; empty-seed first write uses absolute byte cap, not % of 0).
3. **Mandatory re-read** via runtime FSM (pending ranges, not prompt).
4. **One mutating tool call per assistant turn**; `apply_patch` cannot share a
   turn with other tools.
5. **No token-burn arbiter loop.**
6. **Fail→RED** after retry, except TOC §3.6 target-only protection → keep
   Python-delta TOC and still call arbiter.
7. **Chunking** stays one source/target pair; tools do not reopen whole-PR context.
8. **Retry** = full session restart from original draft/seed + empty history.
9. **`finish_reason=tool_calls`** is a normal intermediate; must not map to
   `NON_FINAL` / `EMPTY_TEXT` solely because `content` is null.
10. **Cutover:** after P0+P1, remove whole-file primary path. No production
    `YDBDOC_CRITIC_TOOLS` dual-path. Rollback = revert tip / stop translate.

## Phases

### P0 — Provider + contract freeze (no production loop yet)

1. Independent review of this plan + `REQUIREMENTS_RU.md` §4.1 (**this gate**).
2. Live **capability probe** (paid, gated): one chat with tools against
   DeepSeek V4 Flash on `https://ai.api.cloud.yandex.net/v1/chat/completions`
   using production-shaped auth (`YANDEX_API_KEY` + `YANDEX_FOLDER_ID`).
   Record: HTTP status, whether `tool_calls` present, `finish_reason`, whether
   null `content` appears with tools, multi-turn tool result round-trip, cost.
   Store summary in `knowledge/current-status.md` (no secrets).
3. Freeze tool JSON schemas only if probe forces schema tweaks; finish tool
   already canon in §4.1.
4. Explicit go/no-go on provider tool support before P1c client work.

Exit: written probe result + go/no-go.

**Exit recorded:** PASS / GO in `knowledge/p0-deepseek-tools-probe.md`.

### P1 — TDD offline implementation (main)

Order is vertical slices; each slice: failing tests → minimal code → green
focused suite → commit `main`.

| Slice | Deliverable |
|---|---|
| P1a | Workspace + `apply_patch` / `read` / `grep` pure functions + RO mounts |
| P1b | Tool-loop driver: turn budget + pending_reread FSM + full-session retry |
| P1c | OpenAI client: `tools`, parse `tool_calls`, accept `finish_reason=tool_calls`, multi-turn `role=tool` |
| P1d | Wire critic role to loop; drop whole-file JSON as primary path |
| P1e | Runtime: reviewed bytes from workspace; fail→RED + TOC §3.6 exception |
| P1f | Prompts: critic as editor-with-tools; arbiter untouched |
| P1g | Knowledge + REQUIREMENTS sync if probe tweaked schemas |

Translator, direction, TOC Python delta: **no behavior change** except critic
wiring and TOC exception path.

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

**Not ready for step 6 until** P0 green, P1/P2 offline green, tag pushed, and
Actions still have working `YANDEX_API_KEY` + `YANDEX_FOLDER_ID` (smoke `YC_*`
aliases are not enough for production `doc_translate`).

## TDD test list

### Unit (always on, no network)

1. `apply_patch` applies unified hunk; rejects overlapping/invalid/oversized.
2. `read` returns exact line window with stable 1-based numbers.
3. `grep` returns path+line+snippet; respects workspace bytes not disk.
4. RO mount: `apply_patch` on source/glossary/presentation → invalid.
5. Path escape / unknown path → invalid.
6. Missing required target seeds 0-byte writable file; patch can create content.
7. FSM: patch without subsequent covering `read` → protocol error.
8. FSM: `grep` or second `apply_patch` while `pending_reread` → protocol error.
9. FSM: `apply_patch` bundled with any other tool_call in one turn → protocol error.
10. FSM: parallel `read`/`grep` OK when pending empty.
11. FSM: turn counter hits max → stop with unreviewed failure.
12. FSM: `finish` with pending unre-read patches → error.
13. FSM: retry restarts from original draft bytes (discard partial patches).
14. Client payload includes `tools` for critic role only; translator unchanged.
15. Client parses `tool_calls`; `finish_reason=tool_calls` + `content=null` is
    success intermediate, **not** `NON_FINAL` / `EMPTY_TEXT`.
16. Client round-trips tool results as `role=tool`.
17. Critic success publishes workspace bytes, not assistant prose.
18. Critic fail/503/protocol → RED; arbiter not invoked on those paths
    (extend `test_draft_reviewed_gate`).
19. TOC target-only protection rejects bad patches; after two session failures
    on that class, Python-delta TOC is published and **arbiter is invoked**.
20. Empty-seed / no-op `finish` publishes draft/seed unchanged.

### Integration offline (stub model)

21. End-to-end chunk: stub issues grep → read → apply_patch → read → finish;
    translation branch gets patched file.
22. Stub skips re-read → RED + checkpoint semantics unchanged.
23. Stub exceeds turn budget → RED.
24. Stub tries full-file patch → rejected → retry → RED if persists.
25. Stub creates file from 0-byte seed via patches + covering reads → reviewed.
26. Translator still whole-file JSON; TOC Python delta still applied before
    critic workspace seed.
27. Arbiter still judge-only; no second critic call from findings.
28. History/growth smoke (stub): tool-loop does not re-embed full source/target
    blobs on every turn beyond initial user message + tool payloads.

### Optional live (deselected by default)

29. `@pytest.mark.live` tool-call smoke: real DeepSeek, tiny fixture file,
    assert at least one `tool_calls` round and a successful patch+reread.
    Gate: `YDBDOC_LIVE=1` **and** production-shaped `YANDEX_API_KEY` +
    `YANDEX_FOLDER_ID` (not smoke-only `YC_*` unless the probe script is
    explicitly smoke-scoped and documented as non-runtime).
30. Do **not** auto-run live in CI; cost against grant/budget is manual.

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
| No checked-in proof DeepSeek tools work on YC | P0 probe must create it |
| Personal-shell names (`SINTJURI_*`) appear only in vault daily notes, **not** in this repo | Out of band; do not encode as project contract |

**Not a gap:** production tools feature flag. Dual-path env is **rejected**;
see §4.1 cutover/rollback.

**Ready for live paid critic tests only after:** P0 probe green + operator
exports `YDBDOC_LIVE=1` with production `YANDEX_API_KEY`+`YANDEX_FOLDER_ID`
+ numeric `YDBDOC_DAILY_BUDGET_RUB` / operator spend discipline. Grant-limit
automation is **not** available from repo docs.

**Ready for live BlobDepot re-run (#54888 successor) only after:** P2 delivery
checklist + Actions secrets still valid for `doc_translate` (same `YANDEX_*`).

## Risks

| Risk | Mitigation |
|---|---|
| DeepSeek on YC lacks tool_calls | P0 probe; stop before P1c; renegotiate |
| `finish_reason=tool_calls` misclassified as NON_FINAL / EMPTY_TEXT | Explicit parser tests in P1c; listed above |
| Model loops patches without converging | Turn cap + patch size cap + RED |
| Model “finishes” without re-read | Runtime pending_reread FSM |
| Multi-turn × `reasoning_effort=medium` hits silent wall harder than one-shot | Per-turn max_output_tokens; no full-file re-embed each turn; turn cap; watch wall-clock in P0/P2 |
| Conversation history token growth | Tool results only + compact initial context; audit costs |
| TOC structural damage | Python validator; TOC §3.6 fallback to Python delta → arbiter |
| Dual env names confuse live runs | Prefer `YANDEX_*` for runtime/live; document smoke as non-runtime |
| Silent JSON fallback reintroduced “for safety” | Forbidden by §4.1; rollback = tip revert |
| REQUIREMENTS/code drift on reasoning_effort | Sync in same docs commit; re-check in P1c |

## Acceptance

1. Offline suite green with stub tool model; new witnesses in
   `knowledge/testing.md` checked off.
2. Critic primary path is tools+patches; whole-file `{"files":…}` is not
   production primary (default: **remove** after cutover; no env dual-path).
3. Arbiter unchanged judge-only; no repair loop code paths.
4. Translator + TOC Python path unchanged by behavioral tests except §3.6
   exception wiring.
5. Fail→RED preserved (plus TOC exception).
6. Optional live tool smoke documented and skipped by default.
7. After tag: BlobDepot re-translate from #50839 produces a new PR; residual
   defects are triageable findings, not “critic said fix but bytes untouched”.

## Delivery checklist (end of P2)

1. `git pull --ff-only` `public/main`; work on `main`.
2. Implement P1/P2 with atomic commits **only after** P0 go.
3. Full non-live suite once.
4. Push `public` with `YDB_GH_TOKEN` and cleared `credential.helper`.
5. Tag release tip (move or mint per delivery.md practice).
6. Close stale translation PR / delete `translation/pr-50839` if required.
7. Label `doc_translate` on source [#50839](https://github.com/ydb-platform/ydb/pull/50839).
8. Confirm new open translation PR (successor of #54888 lineage).
9. Independent review of plan **before** step 2; independent review of PR
   **after** step 8.

## Ready for independent review?

**Plan/contract review: addressed.** P0 DeepSeek tool probe recorded **GO**.
P1 offline + P2 live smoke green. Soft tool-error recovery landed from live
failure (`end past EOF`). BlobDepot delivery checklist: tag + close #54888 +
label `doc_translate` on #50839.
