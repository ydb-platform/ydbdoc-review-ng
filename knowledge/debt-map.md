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
> Full non-live pytest earlier: **2222 passed**, 3 deselected (`live`).
> Live `doc_translate` gate still needs credentials / push.

## Neural review triage (2026-10-01)

Findings from `final-clean-context-review.md` + `adversarial-pipeline-review.md`
vs `REQUIREMENTS_RU.md`. Fix only contract violations; skip invented requirements.

| # | Finding | Status |
|---|---|---|
| 1 | GREEN vs unpublished critic bytes (`publication_plan` old base head) | **fixed** |
| 2 | Production chunking lacks prepare_request on RecordedModels | **fixed** |
| 3 | Provider failure one page aborts whole job | **fixed** |
| 4 | Critic failure: one retry; files to arbiter as-is | **fixed** |
| 5 | First push must create translation PR | **fixed** |
| 6 | Markdown/YFM diagnostics block assembled UTF-8 publish | **fixed** |
| 7 | Critic cannot rewrite TOC href/hierarchy or fill null TOC | **fixed** |
| 8 | doc_verify missing target → null to critic | **fixed** (`5e0c140`) |
| 9 | doc_verify only translation PR diff, not full frozen group | **fixed** (`5e0c140`) |
| 10 | Findings with target_line=null not publishable | **fixed** |
| 11 | Zero commits + RED → source report + null checkpoint | **fixed** |
| 12 | Resource/TOC-only PRs rejected after direction | **fixed** (`5e0c140`) |
| 13 | Both-locale changes auto COMPLETE_PAIR | **fixed** (`5e0c140`) |
| 14 | Critic/arbiter missing TOC before/after + binary manifest | deferred |
| 15 | Zero text pairs auto-GREEN without critic/arbiter | **fixed** |
| 16–18 | TOC delta / continue scope_sha issues | deferred |
| 19 | Verify GREEN/YELLOW closes prior RED checkpoint | **fixed** (`4d1b991`) |
| 20 | New translate deletes branch + closes checkpoints | **fixed** (`4d1b991`) |
| 21 | Source echo correction not wired | deferred |
| 22 | Raw Markdown fallback bypasses segment ID contract | **fixed** |
| 23 | Trigger label not removed after acceptance | **fixed** (`4d1b991`) |
| 24 | direction_undetermined → `/ydbdoc continue` | **fixed** (P2) |
| 25 | Strip SHA/internal codes from public comments | **fixed** (P2) |

## Известные расхождения в коде

- Complete-pair Git heuristic removed from direction/mirror; both-locale edits
  stay in translate scope when direction requires it.
- Resource/TOC-only PRs can select direction with empty Markdown inventories and
  plan `COPY_TARGET` / delete / rename for locale assets.
- New `doc_translate` deletes `translation/pr-{n}` and closes open/pending
  checkpoints before snapshot; trigger labels removed after authorize.
- `doc_verify` GREEN/YELLOW closes prior RED checkpoint for the translation.
- Remaining deferred: critic TOC/binary review context (#14), TOC delta §3
  edges (#16–17), continue TOC scope_sha (#18), source-echo correction (#21).
- Offline suite mostly green; **live PR / production `doc_translate` still pending**.
