# Архитектурные инварианты

## Источники данных

- Source читается из immutable snapshot исходного PR или зафиксированного base
  tip по уже реализованным контрактам T003–T006.
- Candidate собирается из source plan, валидных model values и exact protected
  fragments исходника. Старый target не является материалом для склейки.
- URL, path, anchor и код являются protected source fragments. Модель не
  создаёт и не редактирует их.

## Минимальный поток

`doc_translate` идёт линейно: create job audit → authorize → snapshot →
budget → direction/scope → parse → translate → validate/assemble/reparse →
commit/push → critic → optional single repair/revalidate/new commit/final critic
→ PR verdict → terminal job status.

`doc_verify` создаёт job audit, берёт текущую translation branch и authoritative
source, запускает те же validators и critic, при необходимости делает один
repair commit в ту же branch, обновляет verdict и terminal job status. Budget
gate у него отсутствует.

В `v1.0.x` отдельного `doc_continue`, resumable state machine, soft-keep lattice
и navigation overlay нет. Канонические требования задают для следующего релиза
узкий continuation checkpoint только для трёх семантических остановок. Режим
включается в новый action лишь после реализации и независимой приёмки.

## Разделение ответственности

- Локальный детерминированный слой проверяет strict JSON, exact field IDs,
  placeholders/container pairs, source-fragment restoration и повторный parse.
- В `doc_verify` exact protected-fragment invariant отвергает ручное изменение
  URL/path/code относительно authoritative source без navigation graph или link
  resolver.
- Model critic сравнивает authoritative source и final target, проверяя смысл,
  полноту, терминологию и работоспособность ссылок.
- Add source-TOC-reachable страницы добавляет target TOC entry без redirect;
  add вне source TOC не обязан менять TOC; rename обновляет target TOC path и
  создаёт прямой redirect old→new; ordinary edit не меняет ни TOC, ни redirects.
- YDB хранит job и каждую начатую model attempt с request, response при наличии,
  status/error и cost. Job всегда завершается terminal status/error, включая
  ранние failures. TTL текстов составляет 14 дней.
- Budget gate выполняется только перед новым `doc_translate`. Его дневной `SUM`
  включает все известные costs обеих workflow и ролей, в том числе
  `doc_verify` critic/repair; unknown cost не подменяется нулём. Gate идёт после
  authorization/snapshot и до любого model call, включая mixed-locale
  direction call.

## Совместимость

`v1.0.x` фиксирует реализованные контракты `doc_translate` и `doc_verify`.
Дополнительные гарантии можно сохранять, но они не расширяют future scope и не
требуют новых подсистем. `doc_continue` становится частью нового action только
после отдельной реализации и приёмки контракта из канонических требований.
