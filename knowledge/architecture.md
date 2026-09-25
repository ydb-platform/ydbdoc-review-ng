# Архитектурные инварианты

## Источники данных

- Source читается из immutable snapshot исходного PR или зафиксированного base
  tip по уже реализованным контрактам T003–T006.
- Candidate собирается из source plan, валидных model values и exact protected
  fragments исходника. Старый target не является материалом для склейки.
- URL, path, anchor и код являются protected fragments, модель не задаёт их
  содержимое. Внутренний YDB URL меняет только locale, Wikipedia может получить
  официальный target URL через fail-open `langlinks`, остальные значения берутся
  из source. Parse и полный Diplodoc build проверяют результат до публикации.
- Отсутствующая симметричная внутренняя статья становится dependency scope,
  переводится целиком и получает зеркальную TOC-запись. Иной существующий target
  по соответствующей старой ссылке только создаёт `YELLOW` о вероятном
  дубликате; его bytes не участвуют в сборке новой статьи.

## Минимальный поток

`doc_translate` идёт линейно: create job audit → authorize → snapshot →
budget → direction/scope → parse → translate → validate/assemble/reparse →
commit/push → critic-editor → optional revalidate/new commit/final critic
→ PR verdict → terminal job status.

`doc_verify` создаёт job audit, берёт текущую translation branch и authoritative
source, запускает те же validators и critic-editor, при необходимости применяет
его единственное валидное изменённое исправление в ту же branch и только для
него запускает независимый final critic, затем обновляет verdict и terminal job status. Budget
gate у него отсутствует.

`doc_continue` в `1.1.0` создаёт audit, проверяет label actor и последний допустимый
предшествующий `/ydbdoc continue` comment, затем загружает живой checkpoint.
Для translation PR checkpoint выбирается по точным provenance `source_sha` и
текущему `target_sha`; старые открытые checkpoints того же source PR не мешают.
Для source PR неоднозначность остаётся fail-closed.
Replay читает только сохранённые source/base SHA и проверяет scope/field IDs
и exact translation head. Три stage: direction retry, перевод pending документов
с accepted maps и critic-editor только для unresolved review paths. Source-only
assembly и обычные проверки сохраняются. GREEN закрывает checkpoint; повторный
semantic stop наследует первоначальный expiry. Infrastructure failure не является
новым semantic checkpoint. Общей resumable state machine нет.

## Разделение ответственности

- Локальный детерминированный слой проверяет strict JSON, exact field IDs,
  placeholders/container pairs, source-fragment restoration, повторный parse и
  простые build-breaking дефекты Markdown. Потерянные моделью обязательные
  пустые строки вокруг заголовков и списков восстанавливаются во всём candidate,
  если такого дефекта не было в source. Невалидный большой translate chunk
  после одной correction рекурсивно делится по top-level block boundary, пока
  диапазон source blocks строго уменьшается; неделимый невалидный chunk не
  публикуется.
- Publication validator накладывает candidate на trusted base checkout и до
  каждого commit запускает полный официальный Diplodoc build. Любой `ERR` или
  ненулевой exit запрещает публикацию; нефатальный `WARN` не блокирует её.
  Privileged workflow не checkout-ит и не исполняет
  содержимое source PR.
- В `doc_verify` protected-fragment invariant отвергает ручное изменение
  URL/path/code относительно вычисленного ожидаемого значения. Глобальный
  navigation graph не строится; узкий resolver знает только YDB locale и
  Wikipedia `langlinks`, результат проверяет полный Diplodoc build.
- Model critic сравнивает authoritative source и final target, проверяя смысл,
  полноту, терминологию и работоспособность ссылок. Если совместный prompt велик,
  critic получает соответствующие source/target excerpt-пары, а не полный target
  рядом с каждым source excerpt; verdict и findings объединяются.
- Add source-TOC-reachable страницы добавляет target TOC entry без redirect;
  add вне source TOC не обязан менять TOC; rename обновляет target TOC path и
  создаёт прямой redirect old→new; ordinary edit не меняет ни TOC, ни redirects.
- YDB хранит job, versioned continuation checkpoint и каждую начатую model attempt с request, response при наличии,
  status/error и cost. Job всегда завершается terminal status/error, включая
  ранние failures. TTL текстов составляет 14 дней.
- Единственный QA comment является коротким русским пользовательским резюме:
  цветной вердикт, стоимость текущей job, по одному конкретному исправлению на
  файл максимум для десяти файлов и краткая инструкция `doc_continue` при
  семантическом RED. SHA, накопительные
  breakdown и внутренние диагностические данные остаются в YDB и публично не
  выводятся.
- Budget gate выполняется только перед новым `doc_translate`. Его дневной `SUM`
  включает все известные costs трёх workflow и ролей, в том числе
  `doc_verify` critic-editor/final critic; unknown cost не подменяется нулём. Gate идёт после
  authorization/snapshot и до любого model call, включая mixed-locale
  direction call.

## Совместимость

`v1.0.x` фиксирует входы `doc_translate` и `doc_verify`; `1.1.0` добавляет
`continue --pr N` без source/target/budget override. Все режимы возвращают exit 1
при RED. Action проверяет входы по mode до Python. Consumer label template
находится в `docs/examples/doc_continue.yml`; repo-local dispatch workflows
не подписаны на labels чужого репозитория. Реальное подключение consumer и
публикация release tag выполняются отдельно после финальной приёмки.
