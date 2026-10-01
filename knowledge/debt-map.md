# Карта долга кода vs контракт

Сжатый канон: `REQUIREMENTS_RU.md` (commit упрощения контракта).
Цель: удалять старое поведение, не оборачивать compatibility.

## Срезы

| # | Срез | Статус |
|---|---|---|
| 0 | Эта карта | done |
| 1 | Direction = `translation_required` + `direction` + `reason`; Python зеркалит Git | done |
| 2 | Dependency pull A→A1 + лимиты | pending |
| 3 | Whole-file translator, без DocumentChunk split | done |
| 4 | TOC Python-delta + tests | partial |
| 5 | Critic полные файлы → сразу push | pending |
| 6 | Arbiter: YELLOW закрывает checkpoint | done |
| 7 | Continuation state v3 | done |
| 8 | `doc_verify` | pending |
| 9 | Budget / reporting | pending |
| 10 | Full suite + live PR | pending |

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
  Soft-publish частичных translation success до critic ещё не сделан: при fail
  текущий batch целиком уходит в `pending_paths` без байтов в YDB.
- ~~YELLOW в continuation трактуется как незакрытый semantic stop (как RED).~~
  Checkpoint открывает только RED; YELLOW = успех, в QA — руки + `doc_verify`.
- Старые per-pair `DirectionPairVerdict` / `select_direction` живут рядом с inventory classifier.
- TOC §3: append-only `{name,href}` + fail-closed на delete/rename/reorder/hierarchy
  (`tests/unit/test_toc_section3_coverage.py`). Нужен полный structural delta applicator.
