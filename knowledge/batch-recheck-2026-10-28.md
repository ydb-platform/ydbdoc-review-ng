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
