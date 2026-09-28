# Repeat-run build overlay (2026-09-28)

Main/tag `d35aa3b30e873dd70be901c03a4a815f383e08c4`, tool CI green.
Run `36398164442` translated all ten documents, including metrics. Build failed
with YFM003: optimization files not declared in target TOC. No new branch commit.
Root cause: Diplodoc overlay filtered before == after entries against the remote
translation head, although its checkout was clean base. Thus the complete
candidate contained the TOC/assets, but build omitted unchanged entries.
Fix overlays all plan files and builds nonempty unchanged plans. Regression
witnesses cover absent base TOC/binary asset, deletion and checkout restoration.
Release gate: 1948 passed, 3 deselected (133.45 s), Ruff, mypy (49 files),
diff-check, wheel/sdist and installed smoke all green. Real Diplodoc 5.61.0 /
Node 24 build passed on clean source 59c247bb52af966f6801a77d0c28cb73948df93e,
overlaying the published 36-file candidate with 26 unchanged entries. Used one
worker and 768 MB heap caps; checkout git diff empty afterward. New translation
still needs an external run; this local build does not certify old prose quality.

---

# Decimal localization regression (2026-09-28)

Main/tag `08acc684be17ab15a9f560df9330af34a74e86e6`, tool CI green.
Run `36392932909` stopped at metrics chunk5/5 with protected_fragments:field=1
(after source-echo detection/correction). Source SHA
`c58682cd0ca38990b6ed129a9861d5271db9cbd5`.
Seven earlier documents accepted; a pending-translation checkpoint was saved
and activated. Old branch head remains48ab27d, no new PR/result.
Reproduced a concrete parser defect using the metrics decimal values:
Russian1,79 has no PATH, English1.79 incorrectly has PATH, so valid translation
fails with exactly that validation code. Actual private model response was not
retrieved; this is a matching deterministic regression, not a byte-level replay.
Parser fix release gate: 1946 passed, 3 deselected (140.22 s), Ruff,
mypy (49 files), diff-check, wheel/sdist, installed smoke all green. Next: main/tag,
then a fresh doc_translate on source PR50858. Consumer doc_continue still
uses ydb-platform/ydbdoc-review@v0.1.0 (legacy), not NG. The old one-time NG
continue workflow is deleted from main, and the tool repo has no model/YDB
secrets/variables. Do not trigger the legacy continue workflow or change the
consumer CI. The checkpoint remains available, but no configured NG continue
entrypoint is available in this environment.

---

# Critic recovery and untranslated prose (2026-09-28)

Main/tag `9b7ee7c93b6029ebe6bc480a310e6f0fd5a4eaa0` прошёл CI.
Run `36388057226` перевёл документы и успешно собрал Diplodoc с 25 ресурсами.
Опубликована ветка `translation/pr-50858`, head
`48ab27d817cd0183144d2f9f80141bec1addea34`; PR ещё не создан.
Source SHA: `cc87bc31caed5e6e786b2b9aec6474ba59a5e773`.
Review упал на changelog-server: два provider content-filter, затем
QualityExecutionError. В read-only critic отсутствовало adaptive splitting,
хотя critic-editor его поддерживал. Добавлено ограниченное деление пары
source/target без повторов проверенных соседей. Release gate: 1935 passed,
3 deselected (130.40 s), Ruff, mypy (49 files), diff-check, wheel/sdist и
installed smoke translate/verify/continue прошли.
Inspection опубликованного candidate также выявил русские абзацы в пяти
английских документах. Они были model-visible, но translator их скопировал.
Добавлен RU→EN source-echo guard и объяснение для correction prompt. Поэтому
после release gate нужен новый doc_translate, а не только doc_verify старой
ветки. Старый candidate не считается готовым переводом.

---

# Следующий дефект: ресурсы статей (2026-09-28)

Commit `a45326e317163ab689e6837149a9c0d9f0bd551f` опубликован в main и v1.0.1.
CI инструмента зелёный. Run `36383420279` перевёл все 10 документов, включая
все 33 чанка glossary; коррекции и рекурсивное деление успешно восстановили
невалидные ответы. После перевода build остановил публикацию: новые статьи
optimization ссылаются на отсутствующие target SVG/PNG. Translation PR/branch
не создан. Фактический source SHA: `0de6b19c1a686300cf556c58966ad39631ba6b44`.

Read-only probe четырёх статей выявил 25 отсутствующих ресурсов суммарно
1 287 445 bytes. Добавлен общий перенос parser-owned локальных image/download
зависимостей из frozen source и проверки verify/continue. Для сокращения
повторных GET добавлен ограниченный кэш immutable snapshot bytes.
Требования обновлены. Release gate: 1922 passed, 3 deselected (130.01 s);
Ruff, mypy (48 source files), diff-check, wheel/sdist и installed smoke
translate/verify/continue прошли. Следующий шаг: main/tag и новый doc_translate.

---

# Отладка 2026-09-28 (в работе)

Исходный main/tag v1.0.1: `0a64e34ba57c93968c1525ae3e3a039edb724e5a`.
Разобран run `36379127309`, PR #50858: translator и два raw-Markdown critic
ответа на glossary chunk 5/33, затем InvalidTranslationResponse. В публичном
логе нет точной причины валидации или model responses, поэтому конкретные
невалидные bytes из этого run не воспроизведены.

Подтверждены дефекты: две critic-коррекции вместо одной translator-коррекции;
порог adaptive split 4000 при реальных source chunks меньше 3000; отсутствие
кода валидации в chunk trace. Новые regression witnesses падали до изменения
и проходят после. Исправлены mocks, которые молча повторяли последний critic
response и позволяли тестам recursive split проходить без реального деления.
Смысловой critic теперь имеет отдельную настройку модели. Требования обновлены.

Локальные проверки завершены: 1900 passed, 3 deselected (130.78 s), Ruff,
mypy (47 source files), git diff --check, wheel/sdist build, installed smoke
translate + verify + continue без сети. Профильный runtime suite: 111 passed;
continuation + timeout regressions: 102 passed. Полный suite выполнен под
subreaper из-за контейнерного PID 1. Внешний запуск ещё не выполнен; успех
перевода и deployment пока не заявлены.
Ресурсы контролируются отдельным локальным журналом; свободно около 3.7 ГБ RAM
и 28 ГБ диска.

---

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
контекст глоссария ограничивается 8 секциями и 8 000 символами, сначала
выбираются секции с большей лексической релевантностью, затем anchor в
стабильном порядке. Добавлен regression test на лимит и приоритет.

Дальше:

1. Завершить проверки ограничения glossary context: полный suite, Ruff, mypy и
   diff-check.
2. Commit в `main`, push и передвинуть `v1.0.1` на новый кодовый commit.
3. Запустить чистый `doc_translate` повторно.
4. Если создан новый translation PR, независимо проверить перевод, особенно
   `row-oriented tables`, JOIN, dynamic-configuration anchors, glossary anchors

## Проверка лимитной диагностики

Последний запуск `doc_translate` для PR #50858: run `36225170617`. Он завершился
на `prepare` до model calls: фактический scope содержит 8 dependency-файлов и
332142 исходных символа. Значит превышен именно лимит символов 250000, а не
лимит файлов 20. Раньше оба случая ошибочно сообщались как
`scope_limit_exceeded`; отдельного пользовательского комментария при раннем
падении не было, только stderr и job audit.

Добавлены отдельные диагностики `dependency_file_limit_exceeded` и
`source_character_limit_exceeded`, явные русские сообщения CLI и regression tests.

После публикации commit `1eef9a2` и передвижения `v1.0.1` повторный запуск
`doc_translate` получил run `36226050242`. Он также завершился на `prepare` без
model calls, но теперь корректно сообщил в CI: «Перевод остановлен: объём
исходного текста превышает установленный лимит. Разделите перевод на части или
увеличьте лимит». Translation PR не создавался. В текущей интеграции это явное
сообщение есть в логе workflow; отдельный комментарий в исходный PR при раннем
отказе пока не публикуется.
   `operator`/`cardinality`, table singular/plural anchors и симметрию ссылок.
5. Запустить `doc_verify` на новом translation PR.
6. Дождаться зелёных `doc_verify` и `Build documentation` на одном head SHA.

Команды полного pytest описаны в `knowledge/testing.md`. Для mutations в
`ydb-platform/ydb` использовать `GH_TOKEN="$YDB_GH_TOKEN"` при unset
`GITHUB_TOKEN`; значения токенов не печатать и не сохранять.

## Локальная доработка source-only перевода

27 сентября 2026 добавлена локальная, ещё не опубликованная доработка
упрощённого алгоритма. `build_document_prompt` теперь всегда отправляет модели
только полный authoritative source Markdown с защищёнными ссылками, кодом и
YFM-фрагментами. Старый target больше не попадает в translation prompt и
остаётся только для scope/link-проверок. В `doc_translate` и `doc_verify`
рабочий лимит запроса по умолчанию задан workflow-переменной `6000` символов,
при этом unit default сохранён для обратной совместимости тестов.

Live glossary probe на DeepSeek через Yandex Cloud успешно обработал 15 из 15
чанков и восстановил 570 из 570 защищённых фрагментов. Локальные проверки:
runtime focused `105 passed, 10 deselected`, document contract `56 passed`,
probe `5 passed, 1 skipped`, YAML/Python syntax и `git diff --check` зелёные.
Изменения пока не закоммичены и не запушены.

После run `36309411331` установлен root cause его `structure_mismatch`: workflow
использовал runtime default `200000`, потому что вызывающий workflow в
`ydb-platform/ydb` не передавал `YDBDOC_MAX_MODEL_REQUEST_CHARACTERS`. Поэтому
glossary отправлялся восемью большими чанками. Дополнительно локальный parser
помечает валидные source-owned Diplodoc YFM-условия как diagnostics. В итоге
каждый ответ модели мог пройти chunk-level проверку, но финальный assembled
документ отвергался. Runtime default теперь `6000`, а одинаковые source-owned
YFM diagnostic bytes разрешены; новые или изменённые diagnostics по-прежнему
отклоняются. Добавлены regression tests для большого source-only документа,
полной склейки малых чанков и YFM-условий.

Следующий clean run `36316142112` показал ещё один prepare-дефект: bounded
glossary context в 1 500 символов всё ещё мог переполнить 6 000-символьный
prompt вместе с correction note. Контекст теперь динамически ограничивается
через `max_request // 6` (для default 6 000 это 1 000), а terminology test
проверяет малый лимит. Локальный реальный changelog из PR #50858 успешно
готовится в 53 chunk при этом бюджете.
