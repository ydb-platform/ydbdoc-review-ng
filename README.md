# ydbdoc-review-ng

Библиотека и CLI для двунаправленного перевода документации YDB, Python 3.11+.
Поддерживаются два режима: `translate` и `verify`.

Канонический продуктовый контракт находится в `REQUIREMENTS_RU.md`. Каталог
`knowledge/` содержит принятые технические решения и проверенные факты, которые
разработчики используют вместе с конкретным заданием. Код в
`pr_translation_smoke/` является исследовательским прототипом, а не законченной
реализацией production-конвейера.

Реализованы parser, проверка ответов моделей и protected fragments, critic с
одной repair-попыткой, audit/budget adapters, линейная оркестрация и публикация
через внедряемые backend-ы. Поставляемый production factory
`ydbdoc_review_ng.runtime:create_runtime` соединяет GitHub REST/Git Data через
stdlib, Yandex transport, YDB query executor и реальные workflow stages.
Offline tests и installed smoke не вызывают внешние сервисы. Cutover не выполнен.

## Установка

Из корня репозитория, с доступом к индексу пакетов для build/dev dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python scripts/run_with_timeout.py 120 python -m pip install -e '.[dev]'
```

Для установки без сети нужен заранее подготовленный wheelhouse с
`setuptools>=69` и dev dependencies:

```bash
python -m pip install --no-index --find-links "$WHEELHOUSE" 'setuptools>=69'
python -m pip install --no-index --find-links "$WHEELHOUSE" --no-build-isolation -e '.[dev]'
```

Для запуска с внешними сервисами установите runtime extra и выберите factory:

```bash
python -m pip install '.[runtime]'
export YDBDOC_RUNTIME_FACTORY=ydbdoc_review_ng.runtime:create_runtime
```

Единственный SDK runtime extra: `ydb`; базовая зависимость `PyYAML` используется
для безопасного чтения TOC nodes, без конструирования объектов. Для offline-
установки wheelhouse должен содержать эти пакеты и их зависимости. Справка
не импортирует SDK или YAML parser.

## CLI и runtime boundary

Справка работает сразу после установки и не вызывает внешних сервисов:

```bash
ydbdoc-review --help
ydbdoc-review translate --help
ydbdoc-review verify --help
```

Поставляемый factory возвращает `LinearWorkflows` со всеми mandatory adapters.
После настройки credentials, actor allowlist и предварительной установки YDB
schema (отдельно авторизуемая операция) доступны команды:

```bash
ydbdoc-review translate --pr "$SOURCE_PR" --source-sha "$SOURCE_SHA" \
  --budget-rub "$YDBDOC_DAILY_BUDGET_RUB"
ydbdoc-review verify --pr "$TRANSLATION_PR" --source-sha "$SOURCE_SHA" \
  --target-sha "$TARGET_SHA"
```

PR должен быть положительным целым числом, SHA: 40 lowercase hex символов.
`SOURCE_SHA` задаёт authoritative source, `TARGET_SHA` задаёт текущий head
translation branch. Budget является неотрицательным конечным Decimal в RUB.
Verify не имеет budget gate. Без runtime factory команда завершается с кодом 2;
ошибка workflow даёт код 1 и безопасное сообщение. Секреты и transcripts CLI
не печатает. Python callers могут использовать `main(argv, dispatcher=...)`
или `factory=...` без environment factory.

`continue` пока не поддерживается релизом `v1.0.x`. После ошибки исправьте source
и начните новый `translate`, либо вручную исправьте существующую translation
branch и запустите `verify`. Исходный операторский сценарий
`/ydbdoc continue …` задан в `REQUIREMENTS_RU.md` для следующего релиза; до его
приёмки существующий CI `doc_continue` остаётся на прежнем action. Подробнее
об environment и внешних портах:
[deployment boundary](docs/deployment.md).

## Offline-проверки

После установки dev dependencies, из корня репозитория:

```bash
python scripts/run_with_timeout.py 60 python -m ruff check src tests scripts
python scripts/run_with_timeout.py 60 python -m mypy src
python scripts/run_with_timeout.py 180 python -m pytest tests/e2e -q
python scripts/run_with_timeout.py 900 python -m pytest -q -m 'not live'
git diff --check
```

Pytest по умолчанию запрещает sockets, исключает `live` и ограничивает каждый
тест 30 секундами. E2E требует установленный Git, создаёт временные репозитории
и push в локальный bare remote, без внешних API.

## Пакет и GitHub Actions

Для сборки wheel из чистого source tree с уже установленным setuptools:

```bash
python -m pip wheel --no-index --no-deps --no-build-isolation --wheel-dir dist .
```

Wheel содержит `ydbdoc_review_ng` и console entrypoint. Prototype и тесты не
являются runtime пакетом. Версия пакета `1.0.1` соответствует тегу `v1.0.1`.

Два workflow, `.github/workflows/doc_translate.yml` и `doc_verify.yml`, вызывают
[один composite action](.github/actions/doc-review/README.md). Они устанавливают
runtime extra и выбирают поставляемый factory. Проверка actors, snapshots,
модель, metadata producer, публикация, audit и чтение CI checks реализованы.
Внешний вызов использует путь
`ydb-platform/ydbdoc-review-ng/.github/actions/doc-review@v1.0.1`; action всегда
устанавливает пакет из собственного repository checkout, а не из workspace
вызывающего workflow. Credentials, разрешения в `ydb-platform/ydb`, создание
YDB schema и подключение CI к translation head остаются deployment-настройками.
