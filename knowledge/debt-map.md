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
| 8 | doc_verify missing target → null to critic | **fixed** (`4d1b991`/`5e0c140`) |
| 9 | doc_verify only translation PR diff, not full frozen group | **fixed** (`5e0c140`) |
| 10 | Findings with target_line=null not publishable | **fixed** |
| 11 | Zero commits + RED → source report + null checkpoint | **fixed** |
| 12 | Resource/TOC-only PRs rejected after direction | **fixed** (`5e0c140`) |
| 13 | Both-locale changes auto COMPLETE_PAIR | **fixed** (`5e0c140`) |
| 14 | Critic/arbiter missing TOC before/after + binary manifest | **fixed** (`02b3402`) |
| 15 | Zero text pairs auto-GREEN without critic/arbiter | **fixed** |
| 16–17 | TOC delta §3 gaps (prune/delete/conditions/nested labels) | **fixed** (`4184fba`) |
| 18 | doc_continue TOC retranslation breaks scope_sha256 | **fixed** (`7cdf352`) |
| 19 | Verify GREEN/YELLOW closes prior RED checkpoint | **fixed** (`4d1b991`) |
| 20 | New translate deletes branch + closes checkpoints | **fixed** (`4d1b991`) |
| 21 | Source echo correction not wired | **fixed** (echo commit) |
| 22 | Raw Markdown fallback bypasses segment ID contract | **fixed** |
| 23 | Trigger label not removed after acceptance | **fixed** (`4d1b991`) |
| 24 | direction_undetermined → `/ydbdoc continue` | **fixed** (`14d01dd`) |
| 25 | Strip SHA/internal codes from public comments | **fixed** (`14d01dd`) |

## Second external review residuals (ba0acf2, 2026-10-01)

| # | Residual | Status | Notes |
|---|---|---|---|
| 1 | Partial translator success + RED null checkpoint mismatch | **fixed** | Skip null pending equality vs remote bytes |
| 2 | Critic/arbiter NON_FINAL → GREEN / bare abort | **fixed** | NON_FINAL → unreviewed RED with findings |
| 3 | Structure diagnostics block validate/critic publish | **fixed** | Soft-allow structure_mismatch on validate |
| 4 | Zero-commit RED checkpoint not continuable | **fixed** | REVIEW + `target_sha=null` re-translates |
| 5 | Resource path `en/core/core/logo.png` | **fixed** | Locale-root pairing for ASSET/REDIRECTS |
| 6 | Auto-copied binary deps missing from manifest | **fixed** | Candidate assets added to binary_manifest |
| 7 | TOC group delete drops target-only child | **fixed** | Prune overlap, keep unrelated descendants |
| 8 | TOC add existing page / nested ancestor labels | **fixed** | Drop uncovered gate; collect scaffolding strings |
| 9 | DOC_CONTINUE skips first TOC string translate | **fixed** | Skip only `review_documents` continue |
| 10 | GitHub DELETE 204 empty body as error | **fixed** | Accept empty successful response body |
| 11 | Source echo requires full maximal run | **fixed** | ≥32 Cyrillic fragment detection |
| 12 | Label removal swallows 403/5xx/network | **fixed** | Mutation failures terminate |

## Известные расхождения в коде

- Complete-pair Git heuristic removed; both-locale edits stay in translate scope.
- Resource/TOC-only PRs select with empty Markdown inventories and `COPY_TARGET`.
- New `doc_translate` deletes `translation/pr-{n}` and closes checkpoints; labels removed.
- `doc_verify` GREEN/YELLOW closes prior RED checkpoint.
- TOC scope hash ignores volatile `expected_sha256` wording; review-stage continue skips TOC string retranslate.
- Offline suite mostly green; **live PR / production `doc_translate` still pending**.
