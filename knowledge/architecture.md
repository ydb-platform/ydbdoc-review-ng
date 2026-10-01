# Архитектурные инварианты

`REQUIREMENTS_RU.md` — канонический контракт. Этот файл только кратко объясняет
текущую архитектуру. При любом расхождении действуют требования.

## Источники данных

- Runtime фиксирует immutable source/base SHA и читает полные актуальные файлы,
  а не diff и не версии до PR.
- Frozen translation group включает изменения source PR, требуемые
  рекурсивные dependencies, TOC и непреобразуемые assets.
- `YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE` ограничивает всю группу одной
  статьи, включая саму статью. `YDBDOC_MAX_SOURCE_CHARACTERS` ограничивает
  каждый source-файл отдельно.

## Линейный поток

`doc_translate` выполняет authorization, snapshot, budget, direction, scope,
whole-file translation, публикацию первоначального candidate, critic и arbiter.
Critic и arbiter делятся на парные файловые чанки только при
необходимости. Arbiter заново строит чанки по финальным файлам.

`doc_verify` не переводит заново. Он берёт текущий translation head, заново
запускает полные critic и arbiter stages и публикует исправления critic.

`doc_continue` работает только с живым checkpoint и pinned SHA. Успешный
direction retry в том же запуске переходит к scope и translation. Непустые
`pending_paths` имеют приоритет над review findings; после их повтора вся
группа заново проходит critic и arbiter.

## Публикация

- Любой технически извлечённый UTF-8 файл публикуется. Markdown, YFM, YAML,
  TOC, links, anchors, source echo и protected-fragment errors — diagnostics, а не
  publication gates.
- Не публикуется только ответ, из которого strict response parser не
  может технически собрать полный UTF-8 файл.
- Translator публикует все собранные файлы и детерминированные
  delete/rename/resource operations одним первоначальным commit. Каждый
  успешный critic-чанк сразу публикуется отдельным commit.
- Новый `doc_translate` сначала удаляет прежнюю remote translation branch и
  создаёт чистую от pinned base. Первый опубликованный commit создаёт
  новый translation PR.
- Если translator и все critic-чанки не создали ни одного commit,
  пустой PR не создаётся. RED-отчёт и checkpoint с `target_sha = null`
  привязываются к source PR.

## Ответственность

- Python владеет inventory, scope, dependency closure, protected fragments,
  response parsing, TOC structural delta, publication и checkpoints.
- DeepSeek переводит полные файлы, critic возвращает полные
  исправленные файлы, arbiter возвращает только GREEN/YELLOW/RED и findings.
- Arbiter findings не исправляются автоматически и никуда не передаются.
- Build и CI не читаются, не ожидаются и не влияют на semantic verdict.
