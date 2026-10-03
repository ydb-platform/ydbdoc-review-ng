# BlobDepot critic research (local live model)

Date: 2026-10-03. Tip under test evolves from `276cf66` → this branch of fixes.
Live grant credentials: shell `YANDEX_CLOUD_*_DOC_REVIEW` mapped to `YANDEX_*`.
Harness: `scripts/probe_blobdepot_critic_live.py` (draft `#54924` / `e59c027`).

## Why CI said «сбой модели» (#54924 / run 37100488317)

Not the provider. Critic returned **80× HTTP 200 `tool_calls`**. Internal failure:
`LoopFailureReason.TURN_BUDGET` (default 12 turns × 2 session retries) on
`blobdepot.md` and `index.md`. `repair.py` mislabeled that as `provider` →
public text «сбой модели или провайдера». Env `YDBDOC_CRITIC_MAX_TOOL_TURNS`
was never passed into `run_tool_critic_chunk`.

## Local iteration results

| Round | Change | Verdict | Notes |
|---|---|---|---|
| 1 | turns=32, budget label, critic strategy prompt | **RED** | Critic finished all files. Arbiter false positives: TOC `replacing_nodes`, EN selfheal path, RU typo «кана», `--group-ids` |
| 2 | arbiter rules + TOC protection in arbiter context + presentation refs + TOC snapshot keyed by **source** path | **YELLOW** | Only no-op duplicate finding «replace `--snapshot-channel-sp` with `--snapshot-channel-sp`» |
| 3 (parse filter) | drop self-replace / duplicate arbiter findings → GREEN when empty | unit-covered | Makes round-2 payload **GREEN** without another paid call |
| 4 | arbiter default-GREEN prompt; drop bad snippets / «не требуется»; drop source-only TOC demands | **GREEN** live | `artifacts/blobdepot_critic_live_r3.json`; 0 findings |

## What still differs from editorial ideal (even on YELLOW/GREEN)

Reviewed bytes in `artifacts/blobdepot_reviewed/` still show:

- `Blob depot` / `blob depot` instead of canonical **BlobDepot** (glossary PR not on source snapshot; critic did not force CamelCase brand)
- Word order `monitoring page BS_CONTROLLER` / `On the monitoring page BS_CONTROLLER`
- Table cell `CREATED_FAILED` (also in RU source)
- `--group-ids` gloss still `GROUP_ID GROUP_ID list…` (awkward but matches RU placeholders)

These are **not** what made CI RED (budget). They need glossary wiring and/or
deterministic brand normalize + another critic pass focused on presentation.

## Algorithm / prompt / tools / conditions that moved the needle

1. **Conditions:** default `max_tool_turns` 12 → **32**; wire `os.environ` into critic.
2. **Honest labels:** `budget` ≠ `provider`.
3. **Prompt (critic):** finish-early strategy; grep known defect classes first.
4. **Prompt (arbiter):** target-only TOC, EN nav may differ, do not preserve source typos.
5. **Context:** arbiter gets TOC preservation contract + presentation-reference files.
6. **Parse filter:** drop no-op self-replace findings (model noise → false YELLOW).

## Harness usage

```bash
export YDBDOC_LIVE=1
export YANDEX_API_KEY="$YANDEX_CLOUD_API_KEY_DOC_REVIEW"
export YANDEX_FOLDER_ID="$YANDEX_CLOUD_FOLDER_DOC_REVIEW"
export YDBDOC_CRITIC_MAX_TOOL_TURNS=32
.venv/bin/python scripts/probe_blobdepot_critic_live.py
```

Exit 0 on GREEN/YELLOW. Writes `artifacts/blobdepot_critic_live.json` + reviewed files.
