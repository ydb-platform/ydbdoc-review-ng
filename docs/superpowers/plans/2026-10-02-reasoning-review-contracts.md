# Reasoning Review Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the DeepSeek critic and arbiter perform a reasoned, lossless review and preserve useful findings without depending on the BlobDepot glossary PR.

**Architecture:** Keep the existing translator → critic → runtime → arbiter flow. Extend provider-neutral model requests with an optional developer instruction, choose reasoning effort from the model role at the OpenAI boundary, make arbiter coordinates runtime-derived, and reject critic TOC output that removes navigation references unrelated to the source PR delta.

**Tech Stack:** Python 3.11, pytest, OpenAI-compatible Yandex AI Studio API, PyYAML.

**Spec:** `REQUIREMENTS_RU.md` §§3, 4, 4.1, 4.2

## Global Constraints

- Production model remains `deepseek-v4-flash`.
- `reasoning_effort` is `high` only for critic and arbiter and `none` for all other roles.
- No repair loop and no arbiter auto-fix.
- Critic still returns complete corrected files and publishes each successful chunk immediately.
- Work directly in `main`; preserve untracked `.cursor/`.
- Run focused tests per task and the full suite only once at the end.

---

### Task 1: Role-aware reasoning and message separation

**Files:**
- Modify: `src/ydbdoc_review_ng/models/types.py`
- Modify: `src/ydbdoc_review_ng/models/clients.py`
- Modify: `src/ydbdoc_review_ng/quality/critic.py`
- Modify: `src/ydbdoc_review_ng/quality/prompts/critic.txt`
- Modify: `src/ydbdoc_review_ng/quality/prompts/arbiter.txt`
- Test: `tests/unit/models/test_clients.py`
- Test: `tests/unit/quality/test_pr_critic.py`
- Test: `tests/unit/quality/test_pr_arbiter.py`

**Interfaces:**
- `ModelRequest` gains optional `developer_prompt: str | None` without changing existing positional callers.
- OpenAI payload emits developer then user messages and selects role-aware reasoning.
- Critic/arbiter builders put policy in `developer_prompt` and serialized inputs plus final checklist in `prompt`.

- [ ] Add failing payload tests asserting `high` plus developer/user messages for critic/arbiter and `none` plus one user message for translate.
- [ ] Run the focused tests and confirm failures show the current hard-coded `none` and single message.
- [ ] Add `developer_prompt`, role-aware payload construction, and split critic/arbiter request rendering.
- [ ] Update exact prompt tests and run the focused tests green.

### Task 2: Runtime-derived arbiter line numbers

**Files:**
- Modify: `src/ydbdoc_review_ng/quality/critic.py`
- Modify: `src/ydbdoc_review_ng/quality/prompts/arbiter.txt`
- Modify: `src/ydbdoc_review_ng/quality/repair.py`
- Test: `tests/unit/quality/test_pr_arbiter.py`
- Test: `tests/unit/quality/test_critic_arbiter_contracts.py`

**Interfaces:**
- Arbiter JSON findings omit `target_line`.
- `parse_pr_arbiter_response` finds a unique exact snippet in final target bytes and computes the one-based line.
- Missing or ambiguous snippets create a path-specific unreviewed RED finding while valid sibling findings survive.

- [ ] Add failing tests for computed line, ambiguous snippet, missing snippet, and preservation of a valid sibling finding.
- [ ] Run those tests and confirm failures come from the old required `target_line` contract.
- [ ] Change schema, prompt, parser, and result aggregation minimally.
- [ ] Run arbiter and reporting focused tests green.

### Task 3: Accurate unreviewed diagnostics

**Files:**
- Modify: `src/ydbdoc_review_ng/quality/repair.py`
- Test: `tests/unit/quality/test_critic_arbiter_contracts.py`
- Test: `tests/unit/quality/test_critic_chunking.py`

**Interfaces:**
- `_unreviewed_finding` accepts an internal reason category.
- Context overflow, provider/transport failure, invalid structured response, and unresolvable snippet produce distinct Russian public explanations.

- [ ] Add failing tests for each failure source and assert that only context overflow recommends reducing the file.
- [ ] Run focused tests and confirm the current generic message fails them.
- [ ] Thread the reason category through chunk packing and arbiter handling.
- [ ] Run focused tests green.

### Task 4: Preserve target-only TOC navigation

**Files:**
- Modify: `src/ydbdoc_review_ng/toc_delta.py`
- Modify: `src/ydbdoc_review_ng/runtime_content.py`
- Test: `tests/unit/test_runtime_pr_review.py`
- Test: `tests/unit/test_toc_delta_review_witnesses.py`

**Interfaces:**
- A TOC helper identifies navigation references present in the Python-built target TOC but absent from the source-after TOC.
- Critic validation requires those references to remain. Failure uses the existing one retry; after the second failure the Python-built target TOC remains for arbiter.

- [ ] Add a failing production-path test where critic removes `replacing_nodes.md` while editing BlobDepot entries.
- [ ] Confirm the test publishes the deletion before implementation.
- [ ] Add target-only navigation extraction and validate critic TOC output before immediate publication.
- [ ] Run TOC and runtime review focused tests green.

### Task 5: Delivery and live acceptance

**Files:**
- Modify only tests or production files required by failures from Tasks 1–4.

- [ ] Run `git diff --check`, Ruff, mypy, and the complete pytest suite once.
- [ ] Commit and push implementation to `main`; verify remote files and SHA.
- [ ] Move `v1.0.1` to the verified remote SHA.
- [ ] Reapply `doc_translate` to source PR #50839 for a clean restart.
- [ ] Monitor the run through creation of a new translation PR, critic publication, arbiter verdict, and QA comment.
- [ ] Inspect the final translated files for BlobDepot terminology, inline-code, damaged sentences, and preservation of `replacing_nodes.md`; continue only through the supported workflow if verdict is RED.
