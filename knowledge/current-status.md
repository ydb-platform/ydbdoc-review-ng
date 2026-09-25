# Текущий handoff

Обновлено: 2026-09-25. Рабочая ветка: `main`, базовый commit до текущих
незакоммиченных изменений `3c7c8bd80d56d30c161051ab9cb6f199974373e1`.
Перед началом выполнен `git pull --ff-only`, remote был актуален. Каталог
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
5. Если source anchor существует, а target anchor отсутствует, linked article
   добавляется в dependency scope и синхронизируется целиком.
6. Внешние URL неизменны, кроме уже реализованного Wikipedia `langlinks`.
7. Critic-editor делает не более одного исправления полного candidate, после
   чего идут validators, final critic и официальный docs build.

Канонический полный текст находится в `REQUIREMENTS_RU.md`.

## Реализовано, пока не закоммичено

- `terminology.py`: выбирает релевантные парные glossary-секции по терминам.
- glossary context передаётся в translation prompt, critic-editor и final critic.
- `anchors.py`: извлекает explicit и безопасные implicit EN anchors.
- `DependencyLink.fragment` и состояние
  `TARGET_MISSING_ANCHOR_SOURCE_EXISTS`.
- Missing target anchor расширяет dependency scope даже при существующем
  target-файле.
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
58 passed; Ruff, mypy и diff-check зелёные.

Дальше:

1. Получить результат независимого code review текущего diff и исправить только
   доказанные дефекты.
2. Commit в `main`, push, force-move `v1.0.1` на commit и push tag.
3. Закрыть старый translation PR/удалить старую translation branch только в
   рамках ранее подтверждённого clean restart.
4. Запустить новый `doc_translate` для `ydb-platform/ydb#50858`.
5. Проверить новый перевод независимо; при конкретных дефектах исправить
   pipeline или применить один critic-editor pass.
6. Запустить `doc_verify`, дождаться зелёного `Build documentation` на том же SHA.

Команды полного pytest описаны в `knowledge/testing.md`. Для mutations в
`ydb-platform/ydb` использовать `GH_TOKEN="$YDB_GH_TOKEN"` при unset
`GITHUB_TOKEN`; значения токенов не печатать и не сохранять.
