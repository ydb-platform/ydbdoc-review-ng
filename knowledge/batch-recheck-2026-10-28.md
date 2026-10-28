# Повторная проверка девяти PR, 2026-10-28

Повторная метка `doc_translate` добавлена во все девять PR с неудачными запусками 27 октября. Все новые запуски завершены. Action: v1.0.1, код 8061a93.

| PR | CI | Новый комментарий |
| --- | --- | --- |
| [#52478](https://github.com/ydb-platform/ydb/pull/52478) | [success](https://github.com/ydb-platform/ydb/actions/runs/40710543345) | [Перевод не требуется](https://github.com/ydb-platform/ydb/pull/52478#issuecomment-6472355780) |
| [#52520](https://github.com/ydb-platform/ydb/pull/52520) | [failure](https://github.com/ydb-platform/ydb/actions/runs/40710575799) | Нет |
| [#53789](https://github.com/ydb-platform/ydb/pull/53789) | [success](https://github.com/ydb-platform/ydb/actions/runs/40710559007) | [Перевод не требуется](https://github.com/ydb-platform/ydb/pull/53789#issuecomment-6472366161) |
| [#54034](https://github.com/ydb-platform/ydb/pull/54034) | [success](https://github.com/ydb-platform/ydb/actions/runs/40710567153) | [Перевод не требуется](https://github.com/ydb-platform/ydb/pull/54034#issuecomment-6472373984) |
| [#55279](https://github.com/ydb-platform/ydb/pull/55279) | [success](https://github.com/ydb-platform/ydb/actions/runs/40710513076) | [Перевод не требуется](https://github.com/ydb-platform/ydb/pull/55279#issuecomment-6472364924) |
| [#55293](https://github.com/ydb-platform/ydb/pull/55293) | [failure](https://github.com/ydb-platform/ydb/actions/runs/40710506433) | Нет |
| [#55307](https://github.com/ydb-platform/ydb/pull/55307) | [failure](https://github.com/ydb-platform/ydb/actions/runs/40710522731) | Нет |
| [#55308](https://github.com/ydb-platform/ydb/pull/55308) | [success](https://github.com/ydb-platform/ydb/actions/runs/40710549675) | [Перевод не требуется](https://github.com/ydb-platform/ydb/pull/55308#issuecomment-6472364310) |
| [#55457](https://github.com/ydb-platform/ydb/pull/55457) | [success](https://github.com/ydb-platform/ydb/actions/runs/40710533045) | [Перевод не требуется](https://github.com/ydb-platform/ydb/pull/55457#issuecomment-6472349428) |

Успех: 6 из 9. Для всех шести проверено createdAt нового комментария, а не updatedAt исторического.

## Неуспешные запуски

- #52520: обновленный комментарий сообщает `need_context: Whole relevant context exceeds request bound` для `reference/observability/metrics/index.md`. Это локальный предел сериализованного запроса классификатора 64 KiB, не предел токенов провайдера.
- #55307: обновленный комментарий сообщает техническую ошибку классификации. Конкретная причина требует чтения нового лога.
- #55293: предыдущий запуск падал `prepare_source / InvariantViolation` до модели. Новый запуск также failed; отдельное исследование продолжается.

На 14:58 UTC чтение новых логов временно ограничено REST API rate limit, reset 15:02:13 UTC. Статусы получены публичным REST API, комментарии через GraphQL.

## #55293: локально воспроизведённая причина

При обходе зависимостей `build_potential_scopes` до модели парсер падает на
`ydb/docs/ru/core/devops/deployment-options/ansible/initial-deployment/deployment-configuration-v1.md`,
строки 902 и 913 в snapshot `e6c0d1f300a392cd2b1015253b75538e7e19cb66`.

Минимальный пример: `Go to [http://localhost:8765/x](http://localhost:8765/x).`
`build_markdown_plan` воспроизводит `Field.protected_regions: expected ordered non-overlapping contained regions`.
Контрольный пример с подписью `[UI]` вместо URL проходит.

`_inline_regions` предварительно защищает закрывающую часть ссылки `](...)`.
Затем распознавание голого URL в подписи захватывает и эту часть.
Проверяется занятость начального байта, но не всего найденного диапазона.
Получаются пересекающиеся URL и LINK_CLOSE, которые отклоняет Field.

Предложение для обсуждения: ограничить поиск URL ближайшей уже защищённой
границей, сохраняя URL подписи и адрес ссылки. Инвариант не отключать.
Добавить регрессию на минимальный пример и реальный абзац, проверить
сохранение destination, обе локали и пунктуацию. Затем повторить prepare_source
и CI #55293. Пока production-код не изменялся.

Пользователь попросил разбирать три сбоя по очереди и сначала обсудить
исправление #55293. Диагностика #55307 и #52520 отложена.

## Исправление #55293

По согласованию пользователя `_URL.match` ограничен ближайшим уже защищённым
фрагментом. URL подписи больше не поглощает закрывающую часть ссылки.
Проверка Field остаётся без изменений. Добавлены 8 регрессионных случаев:
RU/EN, обычная подпись/URL, точка/двоеточие после ссылки; проверяются точные
защищённые адреса и побайтное восстановление Markdown.
До исправления 4 случая падали с исходным InvariantViolation, 4 контроля проходили.
После исправления все 290 тестов парсера passed, включая новые 8.
Replay обхода зависимостей 13 пар #55293 на snapshot e6c0d1f прошёл успешно.
Полный suite Python 3.11: 272 failed, 2327 passed, 7 deselected; множества
272 failed совпадают с предыдущим baseline, новых ошибок нет.
Первый общий запуск в Python 3.14 остановился при сборе scripts/ из-за
отсутствующего ydb SDK; итоговая проверка проведена в .venv311 на tests/.
Ruff нового теста и git diff --check чистые. Результат CI будет добавлен после запуска.

Независимое ревью выявило лишнее сканирование хвоста абзаца. Исправлено: поиск защищённой границы выполняется только после совпадения URL и внутри его диапазона. Повторные parser/full проверки и replay прошли с теми же результатами.


## Первый CI после исправления парсера

Код `5201704` опубликован в main, v1.0.1 перемещён на него.
[Run 40713615568](https://github.com/ydb-platform/ydb/actions/runs/40713615568)
подтвердил: prepare_source проходит, 11 пар дают no_translation.
Две пары changelog-enterprise.md и changelog-server.md остановились с need_context
из-за локального ограничения64KiB. [Комментарий CI](https://github.com/ydb-platform/ydb/pull/55293#issuecomment-6472844560).
Итог workflow failure; исправление парсера подтверждено, полный результат ещё не достигнут.

Измерение исходных base/head PR: запросы155174/241644bytes. Целые списки,
кандидаты по их общим якорям и повторяющийся контекст раздувают запрос.
Для завершения согласованного полного цикла готовится ограниченная классификация
по частям с покрытием всех изменений; no-op допустим лишь если все части согласованы.

Проверка классификации по частям:42focused tests passed. Реальные payload changelog-enterprise:51275/51488bytes, changelog-server:60064/61410bytes. Это проверка структуры запросов со скриптовым ответом, семантический результат проверяется в CI. Full suite:272 прежних failures, новых нет. Независимое review root/implementation agent проверило покрытие дельт и failclosed aggregation; исправлены закрытие fence и подсчёт omitted sections.


## Проверка направления части

Код1cea901 опубликован и проверен [run40715670900](https://github.com/ydb-platform/ydb/actions/runs/40715670900).
Все запросы прошли лимит, но enterprise получил ru_to_en и workflow RED.
SELECT audit job dd669ba6-481d-4765-ae99-f5c0879acf98 показал противоречие:
EN-only batch получил ru_to_en, хотя reason прямо утверждал, что RU current
уже содержит новые ссылки. Остальные14ответов модели no_translation.

Добавлено ограничение направления по фактической исходной локали дельты
текущей части в schema и проверке ответа. Это не принудительный no-op:
невалидное направление повторяется один раз, затем conflict.
4новые регрессии сначала упали; после исправления47focused tests passed,
mypy module clean. Реальный CI результат записывается после повторного запуска.

Полный прогон после строгой проверки направления выявил некорректные test fixtures: modified source имел identical before/after, либо EN-only classifier отвечал ru_to_en. Исправлены три общие/локальные тестовые заготовки, сохранены проверки актуального target и порядка TOC validation. Итог272failed2341passed7deselected; множество272падений точно совпадает с предыдущим baseline. Новых ошибок нет. Production-проверка не ослаблена.


## Совместный контекст локалей

[Run40718693889](https://github.com/ydb-platform/ydb/actions/runs/40718693889)
после guard вернул en_to_ru для enterprise. По audit job4c5a4211-3d54-4bdd-bd8d-b8e5d80ea269
reason утверждал отсутствие ссылок вRU; проверка показала все14указанных флагов
со ссылками в переданном current_blocks. В одном nested item ссылка стояла на
соседних словах, а не имени флага, но вела на ту же feature_flags.md.
Это не доказательство отсутствующего перевода. Mojibake при первоначальном
просмотре audit вызван экспортом String через json-unicode; после восстановления
байтов latin1→UTF8 русский текст корректен, модель получила исходный UTF8.

Сортировка delta units изменена с(locale,index) на(index,locale), чтобы части
не разделяли все изменения EN от всех RU. Новый тест воспроизвёл прежнее
разделение и прошёл после исправления. Прямой вызов той же реальной модели
с исходными snapshots и настройками CI дал no_translation в обеих частях
enterprise:53114/50349bytes. Модель сама подтвердила согласованность изменений.
Никаких принудительных no-op или пропуска дельт нет.


## Завершение #55293

Код28bab3a опубликован вmain, тегv1.0.1 указывает на него.
[CI40720252201](https://github.com/ydb-platform/ydb/actions/runs/40720252201)
завершился completed/success, оба jobs success.
[Новый комментарий «Перевод не требуется»](https://github.com/ydb-platform/ydb/pull/55293#issuecomment-6473807382)
created_at=updated_at=2026-10-28T15:54:34Z (18:54МСК).
Все13пар получили no_translation, включая обе части каждого changelog.
Это новый комментарий, не редактирование старого сообщения.

Финальные локальные проверки:35тестов классификатора passed,290parser tests
passed ранее для неизменённого исправления URL; full suite272failed2342passed,
7deselected. Имена272failed точно совпадают с baseline, новых падений нет.
Ruff/mypy изменённого классификатора и git diff --check clean.
Независимые ревью не оставили блокирующих замечаний.

Оставшиеся #55307 и #52520 в рамках последовательного разбора не перезапускались
после исправлений #55293, их актуальный итог требует отдельной проверки.


## Анализ следующего PR #55307

[Run40710522731](https://github.com/ydb-platform/ydb/actions/runs/40710522731),
audit jobbc82a791-b77b-4b00-bb9d-56fe706d7c58. Два HTTP400 с одинаковыми
запросами:17305input+262144completion=279449 при общем лимите262144.
Восемь файлов PR:workflows,build.sh,tools/feature_flags README,Python,JSON,
requirements. Локальная classify_path проверка всех8дала outside_locales.

В current main нет раннего no-op для PR полностью вне локалей: пустой список
пар обходит classify_pairs и попадает в прежний inventory_request, который
посылает полные before/after и не задаёт max_output_tokens. Исправленный
контекст/retry путь пары здесь не используется. В runtime.py DIRECTION получает
ExecutionConfig(max_attempts=1), поэтому адаптивный context retry не выполняется;
внешний DirectionClient повторяет исходный запрос. Локальная проверка
_context_retry_body на реальном ответе корректно вычисляет244839, но runtime
не даёт выполнить эту вторую попытку.

Предложение: ранний успешный no-op без модели только когда все изменённые пути
(и старые пути rename) находятся вне обеих локалей. Не путать с отсутствием
Markdown-пар: localized TOC/images/includes могут требовать обработки.
Сообщение:«Перевод не требуется: PR меняет только инструменты и настройки сборки».
Оставшемуся inventory_request задать4096output tokens; повтор direction должен
иметь ограниченный общий бюджет и использовать исправленный запрос при context400.
Тесты: реальные8путей→0model calls+новыйкомментарий; mixed PR не пропускается;
localized metadata-only не пропускается; context retry меняет budget и ограничен.
На этом этапе только анализ, production-код и CI не изменялись.


## Реализация #55307

Для doc_translate PR полностью вне RU/EN завершается штатным no-op без
обращения к модели. Проверяются текущий и прежний путь каждого файла.
Сообщение: «В PR нет изменений файлов RU/EN: перевод не требуется».
Каждый запуск оставляет новый комментарий. Смешанные PR и локализованные
ресурсы продолжают проходить классификацию.

Inventory classifier ограничен 4096 токенами ответа. RecordedModels сохраняет
исправленный после context400 лимит между двумя внешними попытками DIRECTION.
Ключ кеша: SHA256 исходного wire body; значение: только числовой лимит, без
текстов и credentials. Кеш очищается при bind_job и не применяется к другим
ролям. Общее число HTTP-попыток остаётся равным двум.

Тесты проверяют реальные восемь путей #55307, два запуска и два комментария
без модели; mixed PR; rename из локали; бинарный PDF; пагинацию; исправление
контекста между внешними попытками и ограничение двумя HTTP-запросами.
80 целевых тестов passed; один исторически падающий resource-review test
исключён из целевого прогона, но включён в полный. Полный suite: 272 failed,
2350 passed, 7 deselected. Множество падений точно совпадает с предыдущим
baseline (272 failed, 2342 passed), новых падений нет.


## Завершение #55307

Код af46914 опубликован в main, v1.0.1 указывает на него. Независимое review
не нашло блокеров. Повторная постановка doc_translate создала
[run 40729342961](https://github.com/ydb-platform/ydb/actions/runs/40729342961),
итог completed/success. Лог checkout подтверждает SHA af46914bdebf0b04b2240daddc4ae78b724ced29.
[Новый комментарий](https://github.com/ydb-platform/ydb/pull/55307#issuecomment-6475053938)
создан 2026-10-28T16:47:28Z (19:47 МСК): «Перевод не требуется. Причина:
В PR нет изменений файлов RU/EN: перевод не требуется».
Последний проблемный PR #52520 в этом этапе не перезапускался.


## Анализ последнего PR #52520

[Run 40710575799](https://github.com/ydb-platform/ydb/actions/runs/40710575799)
остановился до модели: Whole relevant context exceeds request bound.
PR меняет одну строку topic.read.lag_messages в каждой локали: RATE → GAUGE
и одинаковое пояснение о lag. Для воспроизведения взяты before
828de72529679a1480cca747fb6d325ac5adcfc9 и merged after
8af4af1994bfd99917975aae04d20449af3e611c из истории исходного PR.

pair_request подбирает counterpart blocks по любому совпавшему inline-code
anchor из целого затронутого блока. В данном случае таблица строки161–175
содержит RATE, HIST_RATE, GAUGE: они встречаются в12/6/5блоках, объединение
совпадений захватывает15из37блоков каждой статьи. Вместе с соседями получается
почти вся статья. Unsplit prompt92898bytes превышает64KiB. Только current_blocks
занимают примерно42971RU+28722ENbytes (JSON с пробелами для измерения).

Текущий код после #55293 уже разбивает запрос:58091bytes для EN delta,
44844bytes для RU delta. С fake ответами no_translation оба запроса проходят
и агрегируются в no_translation. Это проверка механики, не реальной модели.
CI и модель для #52520 на этом этапе не запускались.

Предложение: сначала проверить реальный повтор с текущим опубликованным кодом;
для устранения лишнего контекста подбирать counterpart прежде всего по anchors
самого diff, предпочитать редкие/специфичные identifiers (имя метрики), не
расширять выбор по каждому общему типу из целой таблицы. Сохранить целый
затронутый блок и bounded расширение при need_context, без автоматического
no-op по факту изменения двух локалей. Регрессия на снимках этого PR должна
проверить сохранение обеих дельт и нужной таблицы, отсутствие нерелевантных
таблиц, лимит запроса и неоднозначные anchors. Production не изменён.


## Реализация #52520

Предварительный повтор на af46914: [run40735151157](https://github.com/ydb-platform/ydb/actions/runs/40735151157)
success, [новый комментарий](https://github.com/ydb-platform/ydb/pull/52520#issuecomment-6475859355)
создан2026-10-28T17:23:43Z. Ранее добавленное разбиение уже устранило блокировку.

Затем реализован согласованный подбор по anchors изменённых строк с
предпочтением редких совпадений и независимым fallback каждой строки.
Правила зафиксированы в pair-classification.md. RED: два новых теста
воспроизвели oversized request и лишние таблицы; ещё один RED подтвердил
потерю prose-only второго изменения при общем fallback, исправлено.
GREEN:39classifier tests passed; Ruff/mypy изменённого модуля clean.
Full suite272failed2354passed7deselected; множество272падений точно совпало
с baseline после #55307, новых падений нет.
