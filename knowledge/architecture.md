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

`doc_continue` в `1.1.0` создаёт audit, проверяет label actor и последний допустимый
предшествующий `/ydbdoc continue` comment, затем загружает живой checkpoint.
Replay читает только сохранённые source/base SHA и проверяет scope/field IDs
и exact translation head. Три stage: direction retry, перевод pending документов
с accepted maps и critic/одна repair только unresolved review paths. Source-only
assembly и обычные проверки сохраняются. GREEN закрывает checkpoint; повторный
semantic stop наследует первоначальный expiry. Infrastructure failure не является
новым semantic checkpoint. Общей resumable state machine нет.

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
- YDB хранит job, versioned continuation checkpoint и каждую начатую model attempt с request, response при наличии,
  status/error и cost. Job всегда завершается terminal status/error, включая
  ранние failures. TTL текстов составляет 14 дней.
- Budget gate выполняется только перед новым `doc_translate`. Его дневной `SUM`
  включает все известные costs трёх workflow и ролей, в том числе
  `doc_verify` critic/repair; unknown cost не подменяется нулём. Gate идёт после
  authorization/snapshot и до любого model call, включая mixed-locale
  direction call.

## Совместимость

`v1.0.x` фиксирует входы `doc_translate` и `doc_verify`; `1.1.0` добавляет
`continue --pr N` без source/target/budget override. Все режимы возвращают exit 1
при RED. Action проверяет входы по mode до Python. Consumer label template
находится в `docs/examples/doc_continue.yml`; repo-local dispatch workflows
не подписаны на labels чужого репозитория. Реальное подключение consumer и
публикация release tag выполняются отдельно после финальной приёмки.
