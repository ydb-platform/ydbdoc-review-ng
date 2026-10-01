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
> Full non-live pytest: **2222 passed**, 3 deselected (`live`). Continue_review
> timeline/commits и soft-publish/critic-push ожидания выровнены под контракт.
> Остаётся только live `doc_translate` gate (нужны credentials / push).

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
| 10 | Findings with target_line=null not publishable | **fixed** |
| 11 | Zero commits + RED → source report + null checkpoint | **fixed** |
| 15 | Zero text pairs auto-GREEN without critic/arbiter | **fixed** |
| 22 | Raw Markdown fallback bypasses segment ID contract | **fixed** |
| 8 | doc_verify missing target → verification_target_missing | deferred |
| 9 | doc_verify only translation PR diff, not full frozen group | deferred |
| 12 | Resource/TOC-only PRs rejected after direction | deferred |
| 13 | Both-locale changes auto COMPLETE_PAIR | deferred |
| 14 | Critic/arbiter missing TOC before/after + binary manifest | deferred |
| 16–18 | TOC delta / continue scope_sha issues | deferred |
| 19–20 | Verify close RED checkpoint; new translate deletes branch | deferred |
| 21 | Source echo correction not wired | deferred |
| 23 | Trigger label not removed after acceptance | deferred |
| 24–25 | direction_undetermined / strip SHA from comments | deferred (P2) |

## Известные расхождения в коде

- ~~`direction.parse_inventory_response` требует `files[]` с `action`/`toc_delta` от модели.~~
  Direction-only schema landed; Python `mirror_classified_files` владеет actions.
  Complete pairs (оба locale modified/added) помечаются `none` / `COMPLETE_PAIR` из Git.
- `SourceSemanticAction` / `semantic_actions` в inventory ещё пишутся для continue codec (временный bridge).
- ~~`DocumentChunk` / chunk translator в `translation/document.py`.~~
  Один файл = один translate request; adaptive content-filter split удалён.
  Тип `DocumentChunk` ещё живёт как внутренний whole-file контейнер — см. срез ниже.
- ~~`STATE_VERSION = 2` (нужен 3); поля `accepted_documents`, `candidate_sha256`.~~
  State v3: `target_sha` + `pending_paths`/`review_paths`; candidate bytes только с ветки.
- ~~Soft-publish частичных translation success до critic.~~
  `doc_translate` публикует собранный UTF-8 до critic; failed translator paths = JSON null
  в critic payload (не delete в Git). Continue harness ещё умеет открыть translation
  checkpoint через `_legacy_pending_translation_stop` для pending_paths coverage.
- ~~YELLOW в continuation трактуется как незакрытый semantic stop (как RED).~~
  Checkpoint открывает только RED; YELLOW = успех, в QA — руки + `doc_verify`.
- ~~Critic: успешный one-shot чанк сразу commit/push до arbiter.~~
  Context chunking по целым source/target парам + immediate push; пара, которая
  не влезает одна, остаётся unreviewed → RED с null location.
- ~~Старые per-pair `select_direction` / `DirectionModel*` живут рядом с inventory classifier.~~
  Удалены. Остаются bridge-типы `DirectionPairVerdict` /
  `DirectionSelectionResult` для `freeze_scope_manifest`.
- ~~TOC §3: append-only `{name,href}` + fail-closed на delete/rename/reorder/hierarchy.~~
  Structural applicator `toc_delta.apply_toc_delta`: add/delete/rename/change,
  href, hierarchy, includes, conditions; unrelated target entries preserved;
  delete-only without target TOC creates no file. DeepSeek JSON-ID string
  translation for new/changed `name`/`title`/`label`: one retry; map failure →
  TOC pending null, other files continue (same soft-publish path as docs).
- ~~Reporting: public findings capped by files (10), not by findings (25).~~
  QA comment caps at 25 findings with omitted counter; GREEN/YELLOW/RED icons,
  cost (unknown ≠ 0), YELLOW success recipe (`doc_verify`).
- Offline suite green; **live PR / production `doc_translate` still pending**.
