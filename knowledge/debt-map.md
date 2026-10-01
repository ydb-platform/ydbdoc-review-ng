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

> [!success] Offline suite (2026-10-01)
> Focused residual suite after third review: **233 passed**.
> Live `doc_translate` gate still needs credentials / push.

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

## Известные расхождения в коде

- Complete-pair Git heuristic removed; both-locale edits stay in translate scope.
- Resource/TOC-only PRs select with empty Markdown inventories and `COPY_TARGET`.
- New `doc_translate` deletes `translation/pr-{n}` and closes checkpoints; labels removed.
- `doc_verify` GREEN/YELLOW closes prior RED checkpoint.
- TOC scope hash ignores volatile `expected_sha256` wording; review-stage continue skips TOC string retranslate.
- Offline focused suite green; **live PR / production `doc_translate` still pending**.
- Continue tests that still expected per-file `complete_pair` exclusion or translation-PR
  continue after branch-delete/`target_sha=null` were updated to §1.1/§1.2/§5.1 contracts
  (stale expectations, not production bugs).
