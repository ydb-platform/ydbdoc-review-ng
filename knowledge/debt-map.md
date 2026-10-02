# Карта долга кода vs контракт

Сжатый канон: `REQUIREMENTS_RU.md` (CI translation redesign 2026-10-02).
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
| 8 | Soft-publish draft до critic (diagnostics ≠ product) | done |
| 9 | `doc_verify` | done |
| 10 | Budget / reporting | done |
| 11 | Identifier atoms + presentation map + draft/reviewed gate | **done** (P0+P1) |
| 12 | BlobDepot golden harness (P2) | **done** |
| 13 | Full suite after redesign | **done** (2168 passed) |
| 14 | Live PR after redesign | critic fell on 180s timeout; quality hotfix next |
| 15 | Critic HTTP timeout 180→600 + presentation/CLI/CamelCase | **done** (2026-10-02) |
| 16 | Critic mega-request TRANSPORT @ ~270s (provider idle wall) | **done** (2026-10-02): 1 pair/chunk, relevant glossary, max_output_tokens cap |

## CI translation redesign (2026-10-02)

| # | Item | Status | Notes / witness |
|---|---|---|---|
| P0a | Identifier atoms across `\_` | **done** | `tests/unit/parser/test_identifier_atoms.py` |
| P0b | Presentation map from optional old EN | **done** | `tests/unit/translation/test_presentation_map.py` |
| P0c | Critic fail/503 → RED, not arbiter GREEN on raw | **done** | `tests/unit/quality/test_draft_reviewed_gate.py` |
| P1a | Prompt updates + presentation-reference input | **done** | critic/arbiter prompts; translator tag |
| P1b | Canon §1.2/§2/§4.1/§5.1/§7 | **done** | `REQUIREMENTS_RU.md` |
| P2 | Offline BlobDepot golden | **done** | `tests/golden/test_blobdepot_golden.py` + `tests/golden/blobdepot/` |

> [!success] offline suite (2026-10-02 redesign)
> P0+P1+P2 golden landed. Prior full non-live suite was **2168 passed**.
> Live `doc_translate` re-proof on a known source PR is still pending (P2 is
> offline only).

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

## Tip residuals after 6c373d6 (2026-10-01)

Four holes under claimed #6/#7/#17/#18. Tip after first closeout was `cbd0632`.

| # | Bug | Status | Notes / witness |
|---|---|---|---|
| 1 | doc_verify missing TOC → `toc_uncovered` | **fixed** | null `toc_postconditions` + soft-pending; `test_bug1_*` |
| 2 | Critic malformed YAML TOC discarded | **partial → fixed** | Was claimed fixed via `TranslationPlanError` catch, but production `_toc` raises `RuntimeBoundaryError`. Soft-catch RBE in `_validate_toc_correction` + soft TocDeltaError on verify load; `test_a_*` / `test_tip_abc_holes_cbd0632` |
| 3 | Delete-only NON_FINAL continue broken | **fixed** | admit `resource-review` in persistence + continue + restore; `test_delete_only_non_final_checkpoint_is_continuable` |
| 4 | Full source TOC delete | **partial → fixed** | Plan `DELETE_TARGET` worked, but `_pr_review_inputs` re-injected null → force RED. Intentional TOC deletes omitted from required map; `test_b_full_source_toc_delete_accepts_arbiter_green` |

## Tip residual holes after cbd0632 (2026-10-02)

Independent verify + adversary against `cbd0632` found three still-open production holes
(under claimed #2/#4 plus a new continue QA gap).

| # | Hole | Status | Notes / witness |
|---|---|---|---|
| A | Critic/verify TOC soft-publish wrong exception | **fixed** | Catch `RuntimeBoundaryError` from `_toc`; soft-load malformed branch TOC for critic; production-path `test_a_*` |
| B | Intentional TOC DELETE re-injected as required null | **fixed** | Omit `DELETE_TARGET` TOC from `_pr_review_inputs` required map; `test_b_full_source_toc_delete_accepts_arbiter_green` |
| C | GREEN continue noop leaves stale RED QA | **fixed** | Always update source-PR QA on still-zero-commit continue; `test_c_green_continue_noop_updates_stale_red_qa` |

## Tip residuals after A/B/C at 4f4a393 (2026-10-02)

Independent verify of A/B/C **PASS**; adversary found two adjacent lifecycle holes.

| # | Hole | Status | Notes / witness |
|---|---|---|---|
| 1 | `doc_verify` re-requires already-absent intentional TOC delete | **fixed** | Verify `only_targets` demoted source TOC Git delete to `none`, so `DELETE_TARGET` was lost and `load_verification_candidate` re-injected null → force RED + bad checkpoint. Preserve TOC removals through mirror; inventory fallback in `_pr_review_inputs`. `test_verify_already_absent_intentional_toc_delete_*` |
| 2 | Multi-continue leaves stale source-only RED QA | **fixed** | After translation PR exists, reporter updated link only and left zero-commit `<!-- ydbdoc-current-qa -->` RED on source. Neutralize/replace source QA when publishing translation-PR verdict (§7). `test_multi_continue_removes_stale_source_zero_commit_red_qa` |

## Tip lifecycle bugs on 56d6bff (2026-10-02)

Adversary against public/main tip `56d6bff` found three production lifecycle holes.

| # | Hole | Status | Notes / witness |
|---|---|---|---|
| 1 | Repeated verify RED leaves two open checkpoints | **fixed** | Second `doc_verify` RED opened another open row without closing the prior → `/ydbdoc continue` → `continue_checkpoint_missing_or_ambiguous`. Close same-provenance open checkpoints before opening the new RED. `test_repeated_verify_red_keeps_continue_unambiguous` |
| 2 | Stale unmarked «Актуальный…#N» after next translate | **fixed** | Rewriting zero-commit source RED stripped markers, so later clean translate updated only the marked link and left stale #44 text. Rewrite to canonical marked link and update all marked source-link comments. `test_stale_unmarked_aktualny_is_updated_on_next_translation_link` |
| 3 | `doc_verify` never reconciles leftover source RED | **fixed** | Source reconcile ran only for `DOC_TRANSLATE`/`DOC_CONTINUE`. After partial report failure left source RED + translation PR, verify GREEN/YELLOW left source stuck. Include `DOC_VERIFY` in §7 source reconcile. `test_doc_verify_reconciles_leftover_source_red_when_translation_pr_exists` |

## Известные расхождения в коде

- Complete-pair Git heuristic removed; both-locale edits stay in translate scope.
- Resource/TOC-only PRs select with empty Markdown inventories and `COPY_TARGET`.
- New `doc_translate` deletes `translation/pr-{n}` and closes checkpoints; labels removed.
- `doc_verify` GREEN/YELLOW closes prior RED checkpoint.
- TOC scope hash ignores volatile `expected_sha256` wording; review-stage continue skips TOC string retranslate.
- Offline contract surface **READY** including P2 BlobDepot golden; **live PR /
  production `doc_translate` still pending**.
- Continue tests that still expected per-file `complete_pair` exclusion or translation-PR
  continue after branch-delete/`target_sha=null` were updated to §1.1/§1.2/§5.1 contracts
  (stale expectations, not production bugs).
