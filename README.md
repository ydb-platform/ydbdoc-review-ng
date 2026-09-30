# ydbdoc-review-ng

Библиотека и CLI для двунаправленного перевода документации YDB, Python 3.11+.
Версия `1.1.0` поддерживает `translate`, `verify` и `continue`.

Канонический продуктовый контракт находится в `REQUIREMENTS_RU.md`. Каталог
`knowledge/` содержит принятые технические решения и проверенные факты, которые
разработчики используют вместе с конкретным заданием. Код в
`pr_translation_smoke/` является исследовательским прототипом, а не законченной
реализацией production-конвейера.

Реализованы parser, проверка ответов моделей и protected fragments,
audit/budget adapters, линейная оркестрация и атомарная публикация candidate.
Утверждённый 2026-09-30 новый semantic contract ещё не реализован и не проверен
реальными providers: полные актуальные source PR files, полные соответствующие
translation PR files и полный glossary → один critic/editor возвращает полные
исправленные файлы → runtime проверяет и применяет их → независимый arbiter
проверяет окончательный результат. GREEN означает корректный перевод; RED
findings идут непосредственно в отчёт, автоматически не исправляются и никуда
не передаются. Build/CI не участвуют в семантическом вердикте.
Оба review calls получают полный inventory, все review-owned source/candidate
text files и оба полных pinned glossary. Editor возвращает строгую карту всех
editable paths в полные файлы; GREEN требует полного arbiter coverage и
совпадающего digest. Whole-file/source-only translation сохраняется.
Baseline `2a1c268` ещё содержит прежний chunk review. Production работает
через внедряемые backend-ы. Поставляемый production factory
`ydbdoc_review_ng.runtime:create_runtime` соединяет GitHub REST/Git Data через
stdlib, Yandex transport, YDB query executor и реальные workflow stages.
Offline tests и installed smoke не вызывают внешние сервисы. Cutover не выполнен.

## Быстрый probe перевода glossary

Это отдельный исследовательский тест, не часть CI-конвейера. Он получает русский
`glossary.md` из PR #50858, оставляет Markdown-разметку видимой модели, защищает
только ссылки, код и YFM-вставки, переводит документ чанками и сохраняет полный
prompt, ответы и собранный Markdown в `artifacts/glossary-probe/`:

```bash
export YDBDOC_LIVE=1
export YC_API_KEY=...
export YC_FOLDER_ID=...
export YDBDOC_MODEL_TRANSLATE=deepseek-v4-flash/latest
python3 scripts/run_glossary_probe.py
```

Обычные unit-тесты probe запускаются без сети:
`pytest -q -o addopts='' tests/unit/test_glossary_probe.py`.

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
ydbdoc-review continue --help
```

Поставляемый factory возвращает `LinearWorkflows` со всеми mandatory adapters.
После настройки credentials, actor allowlist и предварительной установки YDB
schema (отдельно авторизуемая операция) доступны команды:

```bash
ydbdoc-review translate --pr "$SOURCE_PR" --source-sha "$SOURCE_SHA" \
  --budget-rub "$YDBDOC_DAILY_BUDGET_RUB"
ydbdoc-review verify --pr "$TRANSLATION_PR" --source-sha "$SOURCE_SHA" \
  --target-sha "$TARGET_SHA"
ydbdoc-review continue --pr "$PR"
```

PR должен быть положительным целым числом, SHA: 40 lowercase hex символов.
`SOURCE_SHA` задаёт authoritative source, `TARGET_SHA` задаёт текущий head
translation branch. Budget является неотрицательным конечным Decimal в RUB.
Verify и continue не имеют budget gate; их costs учитываются следующим translate.
Без runtime factory команда завершается с кодом 2;
ошибка workflow или итоговый RED во всех режимах даёт код 1. Секреты и transcripts CLI
не печатает. Python callers могут использовать `main(argv, dispatcher=...)`
или `factory=...` без environment factory.

Для continue разрешённый автор сначала оставляет на source или translation PR
комментарий с первой строкой `/ydbdoc continue` и непустым многострочным контекстом,
затем разрешённый actor ставит label `doc_continue`. Runtime проверяет реальные
issue events, время и автора комментария. Продолжаются только сохранённые
direction, pending translation или RED review checkpoints возрастом до 14 дней.
Source/base SHA берутся из checkpoint; SHA и budget flags запрещены. Уже принятые
maps используются повторно для pending-only translation. По новому контракту
любой semantic review заново охватывает весь PR и glossary; старые chunk-review
checkpoints несовместимы и должны отклоняться fail closed.
GREEN закрывает checkpoint, повторная семантическая остановка сохраняет исходный
expiry. Infrastructure failures требуют нового запуска. Ручная правка translation
branch с последующим verify также допустима. Подробнее об environment и внешних портах:
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
являются runtime пакетом. Версия пакета `1.1.0` предназначена для первого feature
release с continue; тег `v1.1.0` публикуется отдельно после release gate.

Два repo-local workflow_dispatch шаблона, `.github/workflows/doc_translate.yml`
и `doc_verify.yml`, вызывают
[один composite action](.github/actions/doc-review/README.md). Они устанавливают
runtime extra и выбирают поставляемый factory. Проверка actors, snapshots,
модель, metadata producer, публикация и audit реализованы. QA GREEN описывает
semantic качество точного полностью проверенного candidate независимо от external
CI; реализация нового полного PR review остаётся отдельной задачей.
Внешний вызов использует путь
`ydb-platform/ydbdoc-review-ng/.github/actions/doc-review@<reviewed-commit-sha>`; action всегда
устанавливает пакет из собственного repository checkout, а не из workspace
вызывающего workflow. Credentials, разрешения в `ydb-platform/ydb`, создание
YDB schema и подключение CI к translation head остаются deployment-настройками.
Consumer label workflow находится в [шаблоне](docs/examples/doc_continue.yml):
его устанавливают в `ydb-platform/ydb` с reviewed immutable action SHA после
приёмки релиза и миграции schema. В этом репозитории consumer workflow не развёрнут.
