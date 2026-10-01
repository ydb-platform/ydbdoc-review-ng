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
| 4 | TOC Python-delta + tests | partial |
| 5 | Critic полные файлы → сразу push | pending |
| 6 | Arbiter: YELLOW закрывает checkpoint | done |
| 7 | Continuation state v3 | done |
| 8 | Soft-publish частичных translation success до critic | done |
| 9 | `doc_verify` | pending |
| 10 | Budget / reporting | pending |
| 11 | Full suite + live PR | pending |

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
- Старые per-pair `DirectionPairVerdict` / `select_direction` живут рядом с inventory classifier.
- TOC §3: append-only `{name,href}` + fail-closed на delete/rename/reorder/hierarchy
  (`tests/unit/test_toc_section3_coverage.py`). Нужен полный structural delta applicator.
- Critic всё ещё one-shot на весь PR (`quality/repair.py`); нет chunk-by-file-pair
  с immediate commit/push после успешного чанка (§4.1).
