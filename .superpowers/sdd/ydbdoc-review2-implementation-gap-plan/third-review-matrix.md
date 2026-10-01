# Third external review matrix (fix pass)

Дата: 2026-10-01. Base HEAD: `0477139179918c915404e9f79cda5331e8bf262f`.
Canon: `REQUIREMENTS_RU.md` + `knowledge/`.

**READY for residuals 1–9.** Все девять пунктов подтверждены против контракта и
закрыты focused tests + public runtime witnesses. Не найдено «not-a-real-problem»
среди назначенных 1–9: каждый нарушал soft-publish, continue, §4 NON_FINAL,
TOC §3 или §2.2 echo.

## Метод

Прочитаны `REQUIREMENTS_RU.md`, prior `third-review-matrix.md` /
`third-runtime-replay.md`, production paths из списка residuals. TDD: сначала
failing tests, затем минимальные production fixes. Live GitHub/YDB не вызывались.

Focused: **233 passed** (subset including contracts, soft-publish, TOC, language,
checkpoint/continue witnesses).

## Таблица residuals 1–9

| # | Вердикт | Причина / fix |
|---|---|---|
| 1 | **fixed** | `runtime_continue.py` REVIEW: missing branch targets in `review_paths` are soft-publish nulls, not `ContinuationStateError`. |
| 2 | **fixed** | `quality/repair.py`: zero-text critic/arbiter NON_FINAL → `resource_review_unresolved` RED + publishable finding (binary path when present). |
| 3 | **fixed** | Critic/`validate_plan`: all `DocumentTranslationError` (incl. `markdown_invalid` spacing) soft-publish per §2/§4.1. |
| 4 | **fixed** | `restore_accepted_documents` soft-allows structure/Markdown/YAML diagnostics so continue reaches models. |
| 5 | **fixed** | `yaml.YAMLError` caught with other soft diagnostics in `validate_plan` / critic validate / restore. |
| 6 | **fixed** | Null TOC included in reviewable; persistence allows TOC/resource-only REVIEW with empty document scope. |
| 7 | **fixed** | Exact same-name group delete uses `_prune_deleted_descendants` like localized titles (§3.1). |
| 8 | **fixed** | Inserting scaffolding into existing empty target TOC calls `_collect_new_target_strings` (§3.3). |
| 9 | **fixed** | Echo detector: prefix-sum ≥32-letter contiguous windows; no joining Cyrillic across Latin; ~0.0003s on 960 chars. |

## Not-a-real-problem / deferred

| Item | Status | Reason |
|---|---|---|
| (none of 1–9) | — | User asked to mark non-problems explicitly; all nine were real contract bugs. |
| Pre-existing continue tests on main | **deferred** | `test_translation_pr_uses_saved_head…`, `test_direction_selection_excludes_complete_pair…`, `test_selected_no_action_survives…` already fail on clean `0477139` before this pass. |

## Production references

- `runtime_continue.py` — missing-target REVIEW load; TOC/asset reviewable
- `runtime_content.py` — soft-publish validate/restore/critic; null TOC reviewable; asset reviewable
- `quality/repair.py` — zero-text NON_FINAL RED
- `persistence/ydb.py` — TOC/resource-only REVIEW envelope
- `toc_delta.py` — same-name prune; new-target string IDs on insert
- `translation/language.py` — linear contiguous echo windows
