# Что уже существует

Исследованные SHA и ссылки приведены в [источниках](sources.md).
Факты ниже относятся к прочитанным файлам, а не к подтверждённому выполнению
их CI в рамках этого исследования. Реальные модельные вызовы не выполнялись.

## PR #55451 и правила

PR открыт; исследованный head: `df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15`.
Изменены пять файлов под `ydb/docs/`: policy, DOCUMENTATION_RULES,
AGENTS, ydb-documentation skill и README. Нормативный entrypoint находится
именно в `ydb/docs/.ruler/DOCUMENTATION_POLICY.md`.

Policy ссылается на GENERAL_RULES, FORMAT_RULES, DOCUMENTATION_RULES и
задаёт порядок их чтения и приоритет более позднего файла. Это не полный
самодостаточный текст всех правил. DOCUMENTATION_RULES содержит правила
1–15 и таблицу приоритетов. GENERAL_RULES задаёт аудиторию и автоматическую
правку грамматики. FORMAT_RULES задаёт YQL fences, ссылки/якоря и Markdown lint.

В payload исследованного PR author_association равен `CONTRIBUTOR`.
По этому полю нельзя утверждать, что автор не имеет доступа к проекту.
Фактический collaborator-статус автора этого PR отдельно не проверялся.

## Допуск в других CI

В `pr_check.yml` используется `ok-to-test`. Без метки разрешены владелец
репозитория и автор, для которого `repos.checkCollaborator` вернул HTTP 204.
Для вызова используется `GH_PERSONAL_ACCESS_TOKEN`. Учитывается автор PR,
не только тот, кто инициировал событие. При внешнем PR workflow сообщает
об ожидании метки и добавляет `external`.

После gate PR-check удаляет `ok-to-test` и `rebase-and-check`. Это создаёт
гонку для нового workflow, если тот проверяет только живой список меток.
PR-check не содержит отдельного постоянного разрешения для нового ревьювера.

Нерелевантные label events в PR-check получают отдельную concurrency group;
его платная/тестовая работа пропускается, текущий запуск не отменяется.

`cpp_sdk_peerdirs_check.yml` использует другой приближённый допуск:
ветка в upstream, author_association OWNER/MEMBER/COLLABORATOR или метка
`ok-to-test`. Политики разных workflow не полностью одинаковы.
Для нового проекта предложено ориентироваться на API-проверку PR-check.

## Текущий doc_translate

Consumer workflow: `.github/workflows/ydbdoc-review.yml` в ydb.
Событие: `pull_request_target`, только `labeled`, фильтр путей `ydb/docs/**`,
job запускается для `doc_translate`. В consumer нет автоматического запуска
ревью правил при создании PR и нет команды `doc_review`.

Используется action
`ydb-platform/ydbdoc-review-ng/.github/actions/doc-review@v1.0.1`.
На момент исследования tag и main указывают на один SHA:
`16b2b26f141185f1e21620e932ccf334fd244f30`.
Tag является изменяемой ссылкой; это факт данного среза, не гарантия на будущее.

Action имеет `runs.using: composite`, устанавливает Python 3.11 и
`pip install '.[runtime]'`, вызывает CLI `translate`, `verify`, `continue`.
Docker в этом action не используется; Dockerfile в исследованном дереве нет.
Следовательно, контейнер для нового review требуется разработать.

Credentials модели берутся из `YANDEX_CLOUD_FOLDER_DOC_REVIEW` и
`YANDEX_CLOUD_API_KEY_DOC_REVIEW` в consumer, передаются runtime как
`YANDEX_FOLDER_ID` и `YANDEX_API_KEY`. GitHub token и `YDB_SA_KEY` передаются
как env. Runtime проверяет инициатора по `YDBDOC_ALLOWED_ACTORS` и снимает
командную метку. Этот allowlist не решает автоматически задачу допуска
по автору PR и `ok-to-test` нового режима.

Consumer имеет concurrency на PR с отменой предыдущего перевода. После
успешного перевода отдельный job с YDBOT_TOKEN ставит `ok-to-test` на
translation PR. Новый review не должен случайно запускать повторный перевод.

## Модель и деньги

В `RuntimeContent` модель задана непосредственно строкой `deepseek-v4-flash`;
critic_model и arbiter_model получают то же значение. `RecordedModels`
выбирает для DeepSeek Yandex OpenAI-compatible client. `knowledge/model-api.md`
указывает reasoning `none`. Реальный consumer не передаёт иной выбор модели.

В `docs/deployment.md` описаны переменные моделей и другие default values;
они не полностью совпадают с прочитанным runtime. Для спецификации нового
режима приоритет имеет подтверждённый consumer/runtime, а не эта таблица.

`RecordedModels.record` сохраняет каждую попытку и суммирует cost job.
Неизвестная стоимость превращает итог в unknown. Client умеет provider cost
и расчёт по usage; тарифы берутся из реестра `_PRODUCTION_PRICING`.
Это тарифы, записанные в коде, не проверка текущего биллинга provider.

`YDBDOC_DAILY_BUDGET_RUB` ограничивает начало translate: сумма известных
попыток за московский календарный день проверяется перед выполнением.
Запуск, прошедший этот gate, может пересечь лимит и продолжить работу.
Verify и continue такого gate не имеют. Unknown costs в известную дневную
сумму не входят. Механизма жёсткого лимита одного запуска здесь не найдено.

## Документационная сборка

`docs_build.yaml` работает через `pull_request`, с `contents: read`,
без привилегированного `pull_request_target`. Он собирает документацию
Diplodoc и проверяет WARN/ERR. Это отдельная проверка сборки; её успешность
не доказывает соблюдение содержательных правил. Новый reviewer может
использовать проверенную логику ссылок/якорей, но не должен запускать
расширения из PR рядом с секретами модели.

## Переиспользование

В ydbdoc-review-ng уже есть CLI, GitHub REST/Git Data adapters, ModelRequest,
ModelUsage, Decimal pricing, RecordedModels, YDB jobs/attempts, публикация
отчётов и parser Markdown/YFM/links. Банк `knowledge/` и русские требования
уже существуют. Нет самостоятельного production режима проверки нормативных
правил с описанным автозапуском, Docker и бюджетом на каждую попытку.

Существующий semantic review переводов не следует считать реализацией
проверки правил 1–15: у него другая цель и контракт.
