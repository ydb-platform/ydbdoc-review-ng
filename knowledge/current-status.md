# Текущий handoff

Обновлено: 2026-09-25. Рабочая ветка: `main`, актуальный commit
`e6a8047` (`Preserve numeric provider error status`). Он опубликован в
`ydb-platform/ydbdoc-review-ng`; публичный тег `v1.0.1` указывает на этот
последний опубликованный commit. Перед обновлением handoff выполнен
`git pull --ff-only`, remote был
актуален. Каталог
`.worktrees/` является пользовательским untracked-содержимым и не должен
попасть в commit.

## Цель

Получить новый автоматический перевод `ydb-platform/ydb#50858`, независимо
проверить содержание и добиться зелёных `doc_verify` и `Build documentation`
на одном SHA translation PR. Запуск считается завершённым только при наличии
всех четырёх свидетельств.

## Что установлено по последнему переводу

`doc_translate` run `36131827286` создал `ydb-platform/ydb#54159`, head
`9447a0268966d207f28779d6c68cd6e435995d98`, стоимость 1172.8272 RUB.
Независимое ревью дало RED:

- `строковые таблицы` системно стали `string tables`, ожидается термин
  `row-oriented tables`;
- JOIN местами стал `connection`;
- восемь корректных EN anchors dynamic configuration заменились RU anchors;
- новые ссылки на `#operator` и `#cardinality` не имели секций в EN glossary;
- несколько table anchors имели несовпадающую singular/plural форму.

Терминологию обнаружил независимый reviewer, встроенный critic её пропустил.
Причина: переводчик и critic не получали релевантный глоссарий. Ссылки critic
исправить не мог, потому что весь destination скрыт внутри protected placeholder.

## Согласованный алгоритм

1. Переводится целый Markdown-документ; большой документ делится только на
   крупные top-level chunks.
2. Непрозрачные фрагменты защищаются placeholders. Старый target остаётся
   только reference context.
3. Переводчик и critic получают релевантные парные секции glossary, без
   hardcoded словарных замен.
4. Для внутренней ссылки path/query защищены. Fragment обязан существовать в
   target page. Разрешено сохранить валидный target-local fragment старого
   target для той же страницы и исправить только однозначный singular/plural.
5. Если отсутствует сам target-файл, linked article добавляется в dependency
   scope и синхронизируется целиком. Если отсутствует target-anchor, scope
   расширяется только для парного `glossary.md`; это позволяет синхронизировать
   определения `operator`/`cardinality`, не затягивая весь legacy-граф ссылок.
6. Внешние URL неизменны, кроме уже реализованного Wikipedia `langlinks`.
7. Critic-editor делает не более одного исправления полного candidate, после
   чего идут validators, final critic и официальный docs build.

Канонический полный текст находится в `REQUIREMENTS_RU.md`.

## Реализовано и опубликовано

- `terminology.py`: выбирает релевантные парные glossary-секции по терминам.
- glossary context передаётся в translation prompt, critic-editor и final critic.
- `anchors.py`: извлекает explicit и безопасные implicit EN anchors.
- `DependencyLink.fragment` и состояние
  `TARGET_MISSING_ANCHOR_SOURCE_EXISTS`.
- Missing target anchor расширяет dependency scope при существующем
  target-файле только для парного `glossary.md`.
- Существующий target-local anchor используется только для того же внутреннего
  path/query и только если реально существует.
- Однозначный singular/plural anchor исправляется детерминированно.
- `REQUIREMENTS_RU.md` и `knowledge/translation-algorithm.md` обновлены.
- Добавлены unit/integration regression tests.

## Проверки и следующий шаг

Первое независимое review дало FAIL и нашло шесть пробелов: повторная валидация
теряла link overrides, scope не учитывал singular/plural match, override копировал
весь target URL, fragment-only ссылки не проверялись по финальному candidate,
короткий bold-текст давал ложный glossary match, а glossary context не входил в
расчёт размера prompt. Все шесть исправлены и покрыты regression tests.

После исправлений: focused regression suite green, Ruff green, mypy green,
`git diff --check` green. Повторный полный suite: 1856 passed, 2 deselected за
133.10 s. Третий независимый review: PASS. Reviewer отдельно подтвердил, что
неизменённый dangling self-anchor отвергается, absolute RU URL локализуется в EN
без изменения scheme/host/query, singular/plural anchor читается из реальной
target-страницы, а continuation и publication validation не обходят проверку.
Последний полный suite после всех review-fixes: 1857 passed, 2 deselected за
133.05 s; Ruff, mypy и `git diff --check` зелёные.

Первый clean restart после commit `aaf982e` запустил workflow `36151290783`, но
он остановился на prepare за 2m37s с `scope_limit_exceeded`, до model calls.
Причина: missing-anchor closure рекурсивно включал множество legacy-linked
статей из changelog и превысил лимит 20 dependency files. Исправление сужает
автоматическую синхронизацию отсутствующего раздела до парного `glossary.md`;
отсутствующие target-файлы по-прежнему добавляются для любых внутренних ссылок,
а обычные anchors используют валидный старый target-fragment или однозначную
singular/plural-коррекцию. Focused scope/integration suite после исправления:
58 passed; Ruff, mypy и diff-check зелёные. Итоговый полный suite после этого
исправления: 1859 passed, 2 deselected за 130.64 s.

Изменения опубликованы следующими commits:

- `aaf982e` (`Improve translation terminology and link anchors`);
- `dc3c8c4` (`Bound missing-anchor scope to glossary`);
- `191fb24` (`Bound changelog dependency closure`);
- `280bef1` (`Bound model request size independently`).

Старый translation PR `ydb-platform/ydb#54159` закрыт, его ветка
`translation/pr-50858` удалена. Второй clean restart, workflow `36152234777`,
завершился ошибкой за 1m48s на prepare, до model calls. Точный failure code:
`scope_limit_exceeded`, `inventory_files=3`.

Три исходных файла PR: `changelog-enterprise.md`, `changelog-server.md` и
`maintenance/manual/dynamic-config.md`. Лог показывает десятки чтений парных
страниц после сканирования этих документов. Ограничение missing-anchor до
`glossary.md` сработало, но общий closure всё ещё разрастается по
`TARGET_MISSING_SOURCE_EXISTS`: исторические ссылки больших changelog
трактуются так же, как новые зависимости текущего PR. Исправление ограничивает
такое расширение для `changelog-enterprise.md` и `changelog-server.md`, оставляя
dependency audit, но не добавляя их старый граф в текущий перевод. Обычные
документы сохраняют рекурсивный closure. После этого `prepare_source` прошёл,
но первый model-call получил non-retryable HTTP failure (run `36153912925`).
Попытка с лимитом `100000` не дошла до модели: один indivisible top-level
changelog block оказался больше лимита (run `36155088682`). Лимит model request
установлен в `200000`, отдельно от workflow scope limit `250000`; trace теперь
сохраняет безопасный HTTP-код. После перехода на канонический URI
`yandexgpt-5.1` (run `36160140777`) HTTP 400 сохранился на первом chunk.
Диагностика показала ещё один независимый риск: релевантный глоссарий
добавлялся без верхней границы и мог занять почти всё окно модели. Теперь
контекст глоссария ограничивается 12 секциями и 24 000 символами, сначала
выбираются секции с большей лексической релевантностью, затем anchor в
стабильном порядке. Добавлен regression test на лимит и приоритет.

Дальше:

1. Завершить проверки ограничения glossary context: полный suite, Ruff, mypy и
   diff-check.
2. Commit в `main`, push и передвинуть `v1.0.1` на новый кодовый commit.
3. Запустить чистый `doc_translate` повторно.
4. Если создан новый translation PR, независимо проверить перевод, особенно
   `row-oriented tables`, JOIN, dynamic-configuration anchors, glossary anchors
   `operator`/`cardinality`, table singular/plural anchors и симметрию ссылок.
5. Запустить `doc_verify` на новом translation PR.
6. Дождаться зелёных `doc_verify` и `Build documentation` на одном head SHA.

Команды полного pytest описаны в `knowledge/testing.md`. Для mutations в
`ydb-platform/ydb` использовать `GH_TOKEN="$YDB_GH_TOKEN"` при unset
`GITHUB_TOKEN`; значения токенов не печатать и не сохранять.
