# Состояние реализации

Обновлено 8 октября 2026 года.

## Первый рабочий этап

Код первого этапа опубликован напрямую в `main` по указанию заказчика.
[Первоначальный draft PR #2](https://github.com/ydb-platform/ydbdoc-review-ng/pull/2) сохранён как история подготовки.
Код первоначального PR: `ac33a538eb94d599af5c4bb16316e81044d0e53a`.
Первый этап и документы опубликованы в main commit:
`a384f99fd1b54472000c0bc78f03f42500bbd778`.
Base: `16b2b26f141185f1e21620e932ccf334fd244f30`.
Требования и банк знаний также опубликованы в `main`.
Дальнейшие изменения этого проекта публикуются напрямую в `main`, без PR,
пока заказчик не изменит порядок работы.

Реализованы:

- Отдельный пакет `policy_review` и локальная CLI-команда `review`.
- Загрузка полного policy и трёх нормативных файлов.
- Проверка schema/путей/SHA/объёма snapshot.
- MD009, MD032 и язык SQL fences; обработка code fences и YAML frontmatter;
  исключение одинаковых прежних формальных нарушений.
- Общий с переводчиком реестр модели и тарифов.
- Model adapter с отдельным резервом денег перед каждой HTTP-попыткой.
  Unknown cost и сбой аудита запрещают следующие запросы.
- Проверка findings по существующему пути, строке и точной цитате;
  обязательная coverage правил 1–15 в semantic response.
- Offline fixture с правилами из PR #55451 и явно синтетической статьёй.

CLI этого этапа не вызывает модель и не читает её credentials. Model adapter
проверен fake transport. Для платной интеграции обязательны готовый admission
gate и audit callbacks, сохраняющие расходы вне worker. Числовой лимит одного
запуска заказчиком пока не задан; платного значения по умолчанию нет.

## Проверки

139 focused tests прошли: новый пакет, CLI, provider boundary и entrypoints.
Ruff изменённого core/CLI и новых тестов, mypy нового core/configuration прошли.
Локальный smoke: статус `formal_only`, три замечания, стоимость 0 RUB.
Оплачиваемых запросов не было; Docker build не выполнялся.

У исходного репозитория есть отдельные проблемы проверок. На выбранных
90 translation regression tests до и после изменения совпадают 61 падение.
В source до/после совпадают 16 Ruff findings и 55 mypy errors; новых нет.
Три неизменённых bootstrap process-cleanup tests падают в данном MARS runtime.
Полный репозиторий не считается прошедшим все проверки или готовым к release.

## Ожидание новых коммитов и настройка бюджета

По указанию заказчика добавлен `policy_review.debounce.wait_for_quiet_head`:
300 секунд ожидания до дорогих операций, проверка head/status каждые 15 секунд
и в конце, остановка старого ожидания при смене head. Готовность старого head
не даёт права проверить новый. Сбой чтения состояния запрещает запуск.
Добавлены сценарии серии push, смены head на границе срока, closed/draft,
ошибки API и медленного ответа API. Все 52 tests пакета policy_review прошли;
Ruff пакета и его тестов, mypy нового модуля прошли. Пять минут реального
ожидания и платные вызовы в тестах не выполнялись: используется fake clock.

В требованиях согласованы автоматические новые head, 300 секунд тишины,
repository Variable `YDBDOC_REVIEW_MAX_RUN_COST_RUB` в ydb и порядок её создания.
Сам бюджет уже читает одноимённое окружение и не имеет платного default.
Consumer wiring, постоянная lease и production workflow пока не реализованы.
Переменная на GitHub не создавалась: сумму задаёт заказчик. Репозиторий ydb
и release tag в этом изменении не менялись.

## Следующий этап

1. Подтвердить Docker build и реальные YDB transactions через отдельный workflow.
2. Утвердить rules SHA до слияния #55451 либо дождаться main с canonical policy.
3. Применить новую audit schema, выпустить image/action и установить consumer workflow.
4. Добавить полную проверку links/includes/TOC и inline comments/suggestions.
5. Провести ограниченный платный пилот после готовности источника правил и аудита.

Новая GitHub automation, платный режим и перенос tag ещё не выполнены. Ограничения первого этапа описаны в
[docs/policy-review.md](https://github.com/ydb-platform/ydbdoc-review-ng/blob/ac33a538eb94d599af5c4bb16316e81044d0e53a/docs/policy-review.md).


## GitHub/YDB/Docker: следующий этап

Добавлены API-based admission, tickets до concurrency, повторная проверка head
и permissions после debounce, bounded snapshot producer с merge base, полный
content/rules/context и запрет выполнения PR кода. Worker проверяет head/lease
перед каждой оплачиваемой попыткой. Общий комментарий и head Check публикуются
отдельным publisher, marker защищён проверкой автора.

YDB store использует отдельные approvals/claims/leases/runs/attempts tables,
parameterized queries и atomic claim/reserve. Применение schema в production
не выполнялось. Lease с неизвестным расходом не освобождается автоматически.
Подготовлены Dockerfile, hash-locked Linux CPython 3.11 зависимости, dedicated
policy-review action, image build workflow и два consumer шаблона (review/finalize).
Переводческий action и его runtime не изменены.

192 целевых offline tests прошли: 94 теста пакета, provider client, entrypoints и package CLI.
Ruff нового кода/тестов/launcher и mypy нового пакета прошли. Docker и реальные
YDB transactions в MARS не проверены: здесь нет Docker. Подготовлен GitHub build
с anonymous disposable /local integration smoke, без credentials и вызовов модели.
Build workflow, image manifest и результаты сборки проверяются отдельно.

Переменная в ydb проверена через API: лимит 5000 RUB. Это текущая конфигурация
заказчика, не внесённый реализацией default. PR #55451 ещё открыт; canonical policy
в main отсутствует. До слияния требуется отдельное утверждение rules SHA.
Платного пилота, установки consumer workflow в ydb и переноса stable tag пока нет.
Остаются inline comments/suggestions, полный link/include/TOC validator и процедура
сверки незавершённых финансовых резервов. Неполная coverage остаётся явной.

## Проверка выпуска и GitHub Check

Runtime опубликован напрямую в main: `83227d67727a41747e37d8d3138e3f50b166b163`.
[Сборка образа и smoke временной YDB](https://github.com/ydb-platform/ydbdoc-review-ng/actions/runs/37777043461)
запущены через workflow_dispatch; runner ещё не был назначен на момент фиксации.
Успех Docker и реальных SQL transactions пока не подтверждён.

Выявлено, что доступный `YDB_GH_TOKEN` — classic PAT. Публикация Check переведена
на отдельный host transport с нативным Actions token (`checks: write`), как требует
[GitHub Checks API](https://docs.github.com/en/rest/checks/runs#create-a-check-run).
В worker не передаются GitHub credentials. Он проверяет только публичный текущий
head/status через GET PR endpoint; ошибки API запрещают дальнейшие платные попытки.
94 tests нового пакета и 98 целевых совместимых tests прошли; Ruff и mypy прошли.

## Workflow ревью в ydb

PR review и workflow_run finalizer подготовлены в репозитории документации:
[https://github.com/ydb-platform/ydb/pull/55586](https://github.com/ydb-platform/ydb/pull/55586). Это draft PR, а не установленный в main workflow.
Прямой push отклонён правилами ydb: изменения должны идти через PR, нужен
required check checks_integrated. Никакие защиты ветки не обходились.

Jobs отключены до готовности релиза: переменная YDBDOC_REVIEW_ENABLED в ydb
отсутствует. Сборка внешнего image run 37778165215 завершилась failure на шаге
Build immutable worker; Docker/YDB smoke не выполнялись. Сборка образа остаётся
в ydbdoc-review-ng; обработчики PR/завершения помещены в PR репозитория ydb.
Два теста изменённых workflow contracts и Ruff прошли. Модель и production YDB
не вызывались. Выпуск stable tag/образа и включение review по-прежнему не выполнены.

## Подключение по примеру doc_translate

Сверен актуальный main ydb: ydbdoc-review.yml вызывает
.github/actions/doc-review@v1.0.1. Это версионный Git tag. Новый policy-review
в gate/run/finalize подключается по тому же тегу v1.0.1.
Resolver фактической версии также проверяет v1.0.1. Порядок переноса постоянного
тега без правки consumer CI сохраняется; существующий тег пока не перенесён
до image gate. Перенос обновит также переводчик; его совместимость нужно проверить.

Ссылки и credentials обновляются в draft PR ydb #55586. Model API/folder берутся
из тех же YANDEX_CLOUD_API_KEY_DOC_REVIEW / YANDEX_CLOUD_FOLDER_DOC_REVIEW, что у
переводчика. GitHub membership/publication используют native token вне Docker.
Секрет YDB_GH_TOKEN в ydb не требуется; аудит использует существующий YDB_SA_KEY.
193 целевых теста, Ruff и mypy нового пакета прошли; платных запросов не было.
CI остаётся отключённым до готовности image/audit/rules/release.
