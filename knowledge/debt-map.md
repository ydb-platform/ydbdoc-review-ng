# Карта долга кода vs контракт

Сжатый канон: `REQUIREMENTS_RU.md` (commit упрощения контракта).
Цель: удалять старое поведение, не оборачивать compatibility.

## Срезы

| # | Срез | Статус |
|---|---|---|
| 0 | Эта карта | done |
| 1 | Direction = `translation_required` + `direction` + `reason`; Python зеркалит Git | done |
| 2 | Dependency pull A→A1 + лимиты | done |
| 3 | Whole-file translator, без DocumentChunk split | done |
| 4 | TOC Python-delta + tests | done |
| 5 | Critic полные файлы → сразу push + context chunking | done |
| 6 | Arbiter: YELLOW закрывает checkpoint | done |
| 7 | Continuation state v3 | done |
| 8 | Soft-publish частичных translation success до critic | done |
| 9 | `doc_verify` | done |
| 10 | Budget / reporting | done |
| 11 | Full suite + live PR | pending |

> [!success] offline suite (2026-10-01)
> Focused residual suite after five production continue/critic bugs: **164 passed**
> (critic/continue/assets related). Live `doc_translate` gate still needs credentials / push.

## Neural review triage (2026-10-01)

Findings from `final-clean-context-review.md` + `adversarial-pipeline-review.md`
vs `REQUIREMENTS_RU.md`. Fix only contract violations; skip invented requirements.

| # | Finding | Status |
|---|---|---|
| 1–25 | See prior debt-map rows | **fixed** |

## Second external review residuals (ba0acf2, 2026-10-01)

| # | Residual | Status | Notes |
|---|---|---|---|
| 1–12 | See prior second-review table | **fixed** | Soft structure publish, null continue, TOC, echo, labels |

## Third external review residuals (0477139, 2026-10-01)

| # | Residual | Status | Notes |
|---|---|---|---|
| 1 | Missing soft-published target cannot continue | **fixed** | REVIEW continue allows null targets in `review_paths` |
| 2 | Zero-text NON_FINAL → false GREEN | **fixed** | Resource-only NON_FINAL → RED + finding |
| 3 | Critic spacing rejects UTF-8 correction | **fixed** | `markdown_invalid` soft-publishes |
| 4 | Soft-published diagnostic cannot continue | **fixed** | `restore_accepted` soft-allows diagnostics |
| 5 | Malformed YAML publication gate | **fixed** | `yaml.YAMLError` soft-publishes |
| 6 | Zero-commit TOC=null loses checkpoint | **fixed** | Null TOC in reviewable; TOC/resource-only envelope |
| 7 | Same-name TOC group drops target-only child | **fixed** | Exact-identity delete prunes like localized |
| 8 | Empty target TOC ancestor stays RU | **fixed** | `_collect_new_target_strings` on insert |
| 9 | Echo detector cubic + false join | **fixed** | Prefix-sum windows; no Cyrillic rejoin |

## Production continue/critic bugs (bbb7495 → tip, 2026-10-01)

Confirmed vs `REQUIREMENTS_RU.md` after third-review residuals; independent of
complete_pair/no_action/saved_head test rewrites.

| # | Bug | Status | Notes |
|---|---|---|---|
| 1 | Critic malformed YAML silently dropped | **fixed** | `_derive_target_translations` soft→`QualityInputError`; UTF-8 kept for arbiter |
| 2 | Continue after soft-published malformed YAML dies | **fixed** | Same soft derive; `restore_accepted` keeps empty maps |
| 3 | TOC-only `target_sha=null` checkpoint not continuable | **fixed** | Empty Markdown `potential.scopes` allowed on continue |
| 4 | Resource-only RED checkpoint not continuable | **fixed** | Same empty-scope continue admission |
| 5 | Mixed MD+binary continue loads asset as Markdown | **fixed** | Assets skip UTF-8 `AcceptedDocument`; fixed_files bytes only |

## Independent audit (023f662, 2026-10-01)

| # | Finding | Status | Notes |
|---|---|---|---|
| A | Continue still-zero-commit RED → head mismatch + silent report | **fixed** | noop→null checkpoint_sha + source-PR RED report |
| B | `render_report` upgrades arbiter GREEN→YELLOW on probable_duplicates | **fixed** | QA color = arbiter; duplicates stay advisory note |

## Clean-slate review 78e0e58 (2026-10-01)

External Important 1–18 + Minor 19–22 vs `REQUIREMENTS_RU.md`. Tip after closeout:
`947a556` (base was `78e0e58`).

| # | Finding | Status | Notes |
|---|---|---|---|
| 1 | Critic chunk rolls back prior push | **fixed** | accumulate `published_corrections` |
| 2 | Mixed-locale translates unchanged source | **incorrect** | §1.2 both-locale pairs stay in translate scope after complete-pair removal |
| 3 | Delete blocked by TOC link check | **fixed** | §3.5 diagnostic, not gate |
| 4 | New article+TOC can't create target TOC | **fixed** | missing TOC deferred to §3.4 delta |
| 5 | TOC delta drops dependency entry | **fixed** | prefer in-flight metadata draft |
| 6 | doc_verify rejects missing TOC pre-critic | **fixed** | missing TOC → null for critic |
| 7 | Critic TOC UTF-8 blocked by YAML gate | **fixed** | soft-publish any assembled UTF-8 |
| 8 | Null required target can finish GREEN | **fixed** | null → unreviewed RED |
| 9 | Zero-commit silent GREEN | **fixed** | force RED + source report + null SHA |
| 10 | GitHub pagination aborts on rel=next | **fixed** | follow Link pages |
| 11 | Resource-only arbiter empty enum | **fixed** | manifest paths in finding enum |
| 12 | Bash `${…#…}` treated as comment | **fixed** | skip parameter expansions |
| 13 | Delete-only TOC forced as required null | **fixed** | omit intentional no-create |
| 14 | Continue noop nulls existing target_sha | **fixed** | preserve `snapshot.target_sha` |
| 15 | First NON_FINAL forgotten on 2nd fail | **fixed** | `saw_non_final` retained |
| 16 | Incomplete translation not in pending | **fixed** | REVIEW pending + continue translate |
| 17 | resource-review path_mismatch | **fixed** | synthetic path in reviewable |
| 18 | Full source TOC delete not mirrored | **fixed** | Git delete → target TOC delete |
| 19 | Comment-only TOC → unsupported | **fixed** | keep target, zero strings |
| 20 | SQL/YQL comments translated | **fixed** | unsupported language fully opaque |
| 21 | Continue-created PR missing source link | **fixed** | DOC_CONTINUE posts source link |
| 22 | Limit comment missing YDBDOC_MAX_* | **fixed** | variable name in comment |

## Известные расхождения в коде

- Complete-pair Git heuristic removed; both-locale edits stay in translate scope.
- Resource/TOC-only PRs select with empty Markdown inventories and `COPY_TARGET`.
- New `doc_translate` deletes `translation/pr-{n}` and closes checkpoints; labels removed.
- `doc_verify` GREEN/YELLOW closes prior RED checkpoint.
- TOC scope hash ignores volatile `expected_sha256` wording; review-stage continue skips TOC string retranslate.
- Offline contract surface **READY**; **live PR / production `doc_translate` still pending**.
- Continue tests that still expected per-file `complete_pair` exclusion or translation-PR
  continue after branch-delete/`target_sha=null` were updated to §1.1/§1.2/§5.1 contracts
  (stale expectations, not production bugs).
