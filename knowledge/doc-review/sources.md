# Источники и исследованные версии

Дата: 7 октября 2026 года. Доступ: GitHub web и REST API; файлы прочитаны
по зафиксированным SHA. Существующие CI не запускались, модель не вызывалась.

| Срез | SHA |
|---|---|
| PR #55451 head | `df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15` |
| Base SHA из payload PR, срез workflow ydb | `29a91ef4de9fc28ced7949d4a3eecd7d1516f21d` |
| ydbdoc-review-ng main и разрешённый tag v1.0.1 | `16b2b26f141185f1e21620e932ccf334fd244f30` |

## YDB PR и нормативные правила

- [PR #55451: centralize documentation skill rules](https://github.com/ydb-platform/ydb/pull/55451):
  цель централизации, состав изменений и текущий статус.
- [DOCUMENTATION_POLICY.md](https://github.com/ydb-platform/ydb/blob/df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15/ydb/docs/.ruler/DOCUMENTATION_POLICY.md):
  нормативные файлы, порядок чтения, приоритет, запрет дублирования правил.
- [GENERAL_RULES.md](https://github.com/ydb-platform/ydb/blob/df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15/ydb/docs/.ruler/GENERAL_RULES.md):
  аудитория и требование исправлять грамматику/орфографию.
- [FORMAT_RULES.md](https://github.com/ydb-platform/ydb/blob/df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15/ydb/docs/.ruler/FORMAT_RULES.md):
  YQL fences, ссылки/якоря, Markdown lint.
- [DOCUMENTATION_RULES.md](https://github.com/ydb-platform/ydb/blob/df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15/ydb/docs/.ruler/DOCUMENTATION_RULES.md):
  содержательные правила 1–15, приоритеты и полный путь файла в выводе.
- [ydb/docs/README.md](https://github.com/ydb-platform/ydb/blob/df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15/ydb/docs/README.md):
  обслуживание и распространение правил документации.

## Consumer CI в ydb

- [pr_check.yml](https://github.com/ydb-platform/ydb/blob/29a91ef4de9fc28ced7949d4a3eecd7d1516f21d/.github/workflows/pr_check.yml):
  owner/collaborator gate, `ok-to-test`, удаление меток, concurrency
  для нерелевантных label events.
- [cpp_sdk_peerdirs_check.yml](https://github.com/ydb-platform/ydb/blob/29a91ef4de9fc28ced7949d4a3eecd7d1516f21d/.github/workflows/cpp_sdk_peerdirs_check.yml):
  альтернативный gate по upstream/author_association/метке.
- [ydbdoc-review.yml](https://github.com/ydb-platform/ydb/blob/29a91ef4de9fc28ced7949d4a3eecd7d1516f21d/.github/workflows/ydbdoc-review.yml):
  событие `doc_translate`, action v1.0.1, секреты, daily budget,
  immutable source SHA и label translation PR после успеха.
- [ydbdoc-verify.yml](https://github.com/ydb-platform/ydb/blob/29a91ef4de9fc28ced7949d4a3eecd7d1516f21d/.github/workflows/ydbdoc-verify.yml):
  отдельный режим проверки перевода по `doc_verify` и provenance SHA.
- [docs_build.yaml](https://github.com/ydb-platform/ydb/blob/29a91ef4de9fc28ced7949d4a3eecd7d1516f21d/.github/workflows/docs_build.yaml):
  сборка через `pull_request` в контексте с правами чтения.

## Репозиторий реализации

- [ydbdoc-review-ng](https://github.com/ydb-platform/ydbdoc-review-ng):
  существующая отдельная repo; README описывает переводы и текущую структуру.
- [REQUIREMENTS_RU.md](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/REQUIREMENTS_RU.md):
  действующий контракт переводчика, раздел 6: дневной бюджет Europe/Moscow,
  gate перед translate, допущение превышения внутри запущенной задачи.
- [Action doc-review](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/.github/actions/doc-review/action.yml):
  composite action, Python 3.11, установка runtime, CLI, три режима.
- [runtime_content.py](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/src/ydbdoc_review_ng/runtime_content.py#L648):
  RuntimeContent и фактическое значение `deepseek-v4-flash`.
- [runtime.py](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/src/ydbdoc_review_ng/runtime.py):
  `_PRODUCTION_PRICING`, RecordedModels, учёт attempts/cost,
  выбор transport и actor allowlist.
- [models/clients.py](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/src/ydbdoc_review_ng/models/clients.py):
  usage, provider cost, pricing callback, HTTP attempts/retries.
- [models/types.py](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/src/ydbdoc_review_ng/models/types.py):
  ModelRequest/Usage, output limit и Decimal pricing.
- [persistence/ydb.py](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/src/ydbdoc_review_ng/persistence/ydb.py#L921):
  сумма известных costs за московский день и check_daily_budget.
- [application/workflows.py](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/src/ydbdoc_review_ng/application/workflows.py#L517):
  положение бюджетного gate перед выполнением translate.
- [knowledge/model-api.md](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/knowledge/model-api.md):
  production model, reasoning, response contracts и unknown cost.
- [knowledge/INDEX.md](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/knowledge/INDEX.md):
  существующий банк знаний.
- [docs/deployment.md](https://github.com/ydb-platform/ydbdoc-review-ng/blob/16b2b26f141185f1e21620e932ccf334fd244f30/docs/deployment.md):
  описание env; часть значений моделей расходится с фактическим runtime.

## Официальные контракты GitHub

- [pull_request_target](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target):
  событие позволяет обрабатывать PR в контексте основной repo. Недоверенный
  код PR не должен исполняться в этом контексте с секретами.
- [Check repository collaborator](https://docs.github.com/en/rest/collaborators/collaborators#check-if-a-user-is-a-repository-collaborator):
  204 подтверждает статус; для организации включает участников с доступом
  через команды и outside collaborators. Требуется соответствующий token access.
- [Create a Check Run](https://docs.github.com/en/rest/checks/runs#create-a-check-run):
  явный `head_sha` для Check на проверяемом коммите.
- [Workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions):
  отдельные `checks`, `contents`, `issues`, `pull-requests` permissions.

## Границы исследования

Не проверялись фактическая настройка GitHub Variables/Secrets в ydb,
branch protection, доступные квоты и текущие тарифы Yandex Cloud, возможность
provider отменить начатую генерацию и мультимодальность выбранной модели.
Прочитанные runtime constants не заменяют такие интеграционные проверки.
Не запускались сборки, тесты кода, CI, Docker или оплачиваемая модель.
