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

> [!note] Pre-existing continue_review drift
> Несколько integration assertions ещё ждут timeline/commits после critic
> immediate push (`test_critic_editor_*`, pinned branch/publish failures).
> Не регрессия TOC string map; чинить отдельным срезом или вместе с live PR.

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
