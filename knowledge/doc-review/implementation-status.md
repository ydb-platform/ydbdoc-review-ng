# Состояние реализации

Обновлено 8 октября 2026 года.

## Первый рабочий этап

[Draft PR #2: базовый движок проверки правил](https://github.com/ydb-platform/ydbdoc-review-ng/pull/2).
Исследованный и опубликованный commit:
`ac33a538eb94d599af5c4bb16316e81044d0e53a`.
Base: `16b2b26f141185f1e21620e932ccf334fd244f30`.
В main код этого этапа в рамках данной работы не сливался.

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

## Следующий этап

1. GitHub admission: автор/collaborator, оператор метки, `ok-to-test`,
   сохранение разрешения на head SHA, идемпотентность и `doc_review`.
2. Доверенный snapshot producer, ссылки/includes/TOC и полноценный YDB audit.
3. Docker worker, publisher результатов на правильный head SHA и дедупликация.
4. Выпуск образа/action под передвигаемым тегом и consumer workflow в ydb.
5. Ограниченный платный пилот после задания суммы бюджета.

Новая GitHub automation, платный режим, перенос tag и merge этого PR ещё
не выполнены. Ограничения первого этапа описаны в
[docs/policy-review.md](https://github.com/ydb-platform/ydbdoc-review-ng/blob/ac33a538eb94d599af5c4bb16316e81044d0e53a/docs/policy-review.md).
