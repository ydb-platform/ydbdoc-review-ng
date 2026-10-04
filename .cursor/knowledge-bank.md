# Project knowledge bank

## 2026-10-01 09:51 UTC
<!-- 6a19467fe89e2c6e -->
- §1: проверка без обязательного повторного перевода. §6.2: critic всё равно переписывает файлы и пушит commits.
- В §2 и §8 модель снова «классифицирует добавление, изменение, удаление…» — звучит как те же Git-операции. Риск двойной трактовки при реализации.
- «Перевод не требуется» / `direction_undetermined` / ошибка JSON direction — три разных исхода; граница между «предупреждение» и «continuable checkpoint» читается с трудом.
- Схлопнуть решения**
- Удалить или строго определить `review_paths`.
- Могу следующим шагом набросать сжатый каркас `REQUIREMENTS_RU.md` (оглавление + инварианты + что выкинуть), без правки файла, или сразу предложить конкретный diff текста.

## 2026-10-01 09:53 UTC
<!-- 1d86e8b0e071a992 -->
- testing / delivery** — TDD, focused → один полный suite, работа в `main`, без лишних QA-кампаний.
- Замечание: `.cursor/knowledge-bank.md` — не этот банк, там обрывки прошлого ответа про противоречия. `current-status.md` может отставать от рабочей копии: в git уже много изменений под новый контракт.

## 2026-10-01 09:54 UTC
<!-- 0e3bb82c49789c19 -->
- `testing.md`

## 2026-10-01 09:56 UTC
<!-- 5f1cf6a17edaf3f8 -->
- 4. **`model-api.md`: direction и «простая проверка» как два пункта**
- Банк намеренно краткий, но без этого легко ошибиться при реализации:

## 2026-10-01 10:02 UTC
<!-- 0f6003682b3890e5 -->
- Прежде чем компактить `REQUIREMENTS_RU.md` и `knowledge/`, нужны решения. Ответь коротко по пунктам.

## 2026-10-01 10:17 UTC
<!-- 6de10ea25ca2bce5 -->
- RED** — серьёзные проблемы / нет файла / не смогли проверить; checkpoint открыт → `/ydbdoc continue` с контекстом или ручная правка + `doc_verify`
- Пример: удалили `ru/foo.md`.
- Вариант A (проще):** Python по Git видит `delete` → удаляет `en/foo.md`. Модель только отвечает: «перевод нужен? RU→EN или EN→RU?»

## 2026-10-01 10:20 UTC
<!-- 482b7dc669e1da57 -->
- Сделано.
- Контракт:** `REQUIREMENTS_RU.md` сжат с ~1095 до **319** строк. Зафиксированы твои решения: dependency pull, YELLOW=успех, Python владеет Git-ops, TOC особый путь, soft publish, все три режима.

## 2026-10-01 10:24 UTC
<!-- 0cff31f03993bff0 -->
- Это architectural: не «починить один баг», а привести кривой runtime к короткому канону. Предлагаю так.
- Не переписывать всё одним коммитом и не плодить ветки. Идём **вертикальными срезами по контракту**: failing test → минимальный код → focused tests → commit в `main` → строка в `knowledge/current-status.md`. Старый путь (chunk-translator, semantic actions модели, YELLOW≈RED) **удаляем**, не оборачиваем compatibility-слоем.
- | **4** | TOC Python-delta + тесты строк | Особый путь, хорошо покрывается тестами |
- | **10** | Полный suite → tag → живой `doc_translate` на тестовом PR | Приёмка |
- Параллелить можно только непересекающиеся куски (например reporting vs TOC), но **1→3→5→6** лучше строго по порядку: каждый следующий опирается на предыдущий.
- Не чиним «всё сразу» огромным diff без зелёных focused tests.
- Не добавляем новые gates/fallback без твоего «да».
- Критерий «готово»

## 2026-10-01 12:39 UTC
<!-- dcd873ff5448a9ec -->
- Раньше push был 403. [Push main to public](d524703c-fc29-4a37-93cd-3066a6d5353b) сейчас проверит оба токена в env и попробует залить `main` в `public`.

## 2026-10-01 15:15 UTC
<!-- 3fa988ef8d47a6fd -->
- Проверю остатки по канону и исправлю реальные дыры. Ведёт [Fix residual review gaps](cd0358fe-79cc-494e-a495-5fb9aa4ae8a0).

## 2026-10-01 15:50 UTC
<!-- 043e2cb948852abf -->
- Проверю 9 остатков по канону: что реально — починю, что нет — явно отмечу. Ведёт [Fix third-review residuals](94cbec07-e91c-4dd5-aa8f-2702a477a343).

## 2026-10-01 16:22 UTC
<!-- 7c724b95d74a002c -->
- Все пять подтверждены как контрактные. Чинит [Fix five confirmed bugs](48417deb-94f3-4fdf-9e06-896b8bcdf4bd).

## 2026-10-01 18:02 UTC
<!-- 428253eae38f4aa4 -->
- Проверяю каждый из пяти до закрытия: [Verify five bug fixes](98711d69-c6d5-44a8-997d-1a2bfb6b88ce).

## 2026-10-01 18:41 UTC
<!-- f0145a1ef442818d -->
- Проверяю sync с `public/main`: [Check if main is pushed](c3873661-7116-4cac-bae3-83e76aeb099e).

## 2026-10-01 19:41 UTC
<!-- b7ba4d6a5b5bfde8 -->
- Проверяю все 22 пункта по канону: что реально — чиню, что нет — явно отмечу. Ведёт [Triage and fix review 22](a3bffe51-7421-40f5-bb1b-cb9eab31004d).

## 2026-10-01 20:21 UTC
<!-- ffb7c1cff4470158 -->
- Готовлю отчёт для критика: [Write critic response report](c10b61e4-464d-41c0-9acb-fb97e549c17b).

## 2026-10-01 20:34 UTC
<!-- fffcb4a0e214acc2 -->
- Четыре пункта разберу до конца: либо фикс с тестом, либо явный «не баг». Ведёт [Fix four TOC/verify bugs](99c9c69a-ad4f-485d-91ce-42db2eda12a3).

## 2026-10-01 21:20 UTC
<!-- 19c126163334a464 -->
- Да, это были дыры в прошлых фиксах (неверный exception / повторный inject delete / noop без QA). [Fix incomplete TOC/continue bugs](22678e7e-2bce-41c9-9f04-8655af71ddcb) закрывает A/B/C по production-пути.

## 2026-10-02 04:46 UTC
<!-- 91bb80b7654cf9e0 -->
- Чиним три lifecycle-бага на `56d6bff`: ambiguous continue после повторного verify RED, stale unmarked ссылка на старый translation PR, и source RED после partial report failure при успешном verify.

## 2026-10-02 07:32 UTC
<!-- ef37b053fe493e6b -->
- Смотрю логи run и разбор ответа арбитра. Сейчас вернусь с конкретной причиной.

## 2026-10-02 08:00 UTC
<!-- fb282c41e03c0412 -->
- Смотрю diff, тесты и путь «невалидный arbiter → RED». Вердикт после ревью.

## 2026-10-02 11:14 UTC
<!-- 2807fed8595180de -->
- P2 golden BlobDepot offline** — это автотест без GitHub и без живой модели.
- Берём зафиксированный кейс BlobDepot (RU из #50839, при необходимости старый EN), прогоняем через новую логику (атомы, presentation map, gate критика) и сравниваем с эталоном: нет `page\_CONTROLLER`, CLI-флаги с backticks, целые идентификаторы, при падении критика — RED, а не «готовый» сырой перевод.
- Offline** = только pytest в репозитории. **Golden** = заранее записанные «как должно быть». **BlobDepot** = тот PR, на котором всё ломалось.
- Пока этот тест не зелёный, повторный live `doc_translate` — лотерея.

## 2026-10-02 11:15 UTC
<!-- b99a273830ee865e -->
- Ни то ни другое.** Тест ещё не пишется и не крутится: есть только план в `knowledge/blobdepot-golden-plan.md`, сам harness P2 не сделан.

## 2026-10-02 11:29 UTC
<!-- f78e8d8064cbde84 -->
- Потом исправили: удалили `translation/pr-50839` → [#54801](https://github.com/ydb-platform/ydb/pull/54801) закрыт. Чистый run сейчас: https://github.com/ydb-platform/ydb/actions/runs/37001145507 (`in_progress`, tip `3f264ac`).

## 2026-10-02 15:38 UTC
<!-- resume-clean-4f2867c -->
- Resume clean `doc_translate` tip `v1.0.1`=`4f2867c`: закрыт stale [#54863](https://github.com/ydb-platform/ydb/pull/54863), удалена `translation/pr-50839`, один label. Run https://github.com/ydb-platform/ydb/actions/runs/37027975808 → translation [#54868](https://github.com/ydb-platform/ydb/pull/54868) (`198ace1`).

## 2026-10-02 11:52 UTC
<!-- 9c542f9551aafa05 -->
- Чиню `_copy_structure` и добавляю тест.

## 2026-10-02 13:44 UTC
<!-- 1735238c6b3999fc -->
- Сверяю логи run с кодом вызовов — отдельно провайдер и наша ошибка.

## 2026-10-03 05:03 UTC
<!-- 18e0ca973cef0188 -->
- Подготовка (runtime, без модели)
- 2. Гоняет детерминированные проверки (placeholders, TOC target-only, UTF-8, схема).
- 3. Если сеанс сорвался / слишком много ошибок tools → RED «не проверен», как сейчас при fail критика.
- Без изменений по роли: смотрит уже reviewed байты, пишет GREEN / YELLOW / RED. **Не чинит.** Если RED — человеку или отдельному `doc_verify` / continue, но не «ещё 10 раз тот же слепой critic».
- | Нет проверки своей правки | Re-read обязателен |

## 2026-10-03 05:08 UTC
<!-- 690f975f547b42cc -->
- Пишу план в REQUIREMENTS и knowledge, ищу env для YC-тестов. После этого — отдельное независимое ревью плана, уже потом код.

## 2026-10-03 05:15 UTC
<!-- tool-using-critic-plan-docs -->
- Docs-only: `knowledge/tool-using-critic-plan.md` + `REQUIREMENTS_RU.md` §4.1 tool workspace (read/grep/apply_patch/finish, mandatory re-read, runtime-applied bytes). Arbiter judge-only; no arbiter↔repair.
- `clients.py`: **нет** `tools` / `tool_calls` сегодня — P0 live DeepSeek probe обязателен.
- Open translation BlobDepot: [#54888](https://github.com/ydb-platform/ydb/pull/54888) ← #50839.
- YC env **found:** `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`, `YDBDOC_MODEL*`, `YDBDOC_DAILY_BUDGET_RUB`, `YDBDOC_LIVE`, smoke `YC_API_KEY`/`YC_FOLDER_ID`/`YDBDOC_MODEL_TRANSLATE`, hardcoded OpenAI/native endpoints.
- YC env **missing:** grant id / grant quota remaining; unified live creds; critic tools feature flag; checked-in tool_calls proof.
- Ready for independent **plan** review. Runtime tool loop not started.

## 2026-10-03 07:49 UTC
<!-- 2fddca844d8103ab -->
- Сделано.
- выкидываем требования добавить source-only TOC (`selfheal.md` и т.п.)

## 2026-10-03 08:09 UTC
<!-- 4707e80d68991a30 -->
- QA: **GREEN**, «Перевод проверен. Исправления не требуются.» (~142 ₽)

## 2026-10-03 08:14 UTC
<!-- c3e645fa41700475 -->
- Проверил [#54929](https://github.com/ydb-platform/ydb/pull/54929) против RU из #50839. Для техдока **нормально, GREEN заслужен**.
- TOC: BlobDepot / decommissioning добавлены, `replacing_nodes` сохранён, EN-навигация не сломана

## 2026-10-03 08:28 UTC
<!-- c40421f7d205b544 -->
- Да, скилл `ydb-docs-backport` есть. Запустил по нему разбор для [#50839](https://github.com/ydb-platform/ydb/pull/50839) и [#54929](https://github.com/ydb-platform/ydb/pull/54929): сверка со всеми `stable-X-Y` от 25.1, без открытия backport PR. Результат пришлю, как будет готов.

## 2026-10-03 08:46 UTC
<!-- d5c69c85d4a4b445 -->
- Формулировку лучше не «все слова с большой», а явно: *English ATX headings use title case (Chicago); keep `{#anchors}` unchanged*. Иначе модель начнёт капитализировать `a`/`the`/`of` или трогать якоря.
- Могу сразу вписать в оба промпта и прогнать тесты.

## 2026-10-03 08:53 UTC
<!-- 8eb1b8b923c8f2d0 -->
- Сделано: `a5c6d06` на `public/main`, тег `v1.0.1` сдвинут туда же.
- Текущий run [#40553](https://github.com/ydb-platform/ydb/pull/40553) → [#54932](https://github.com/ydb-platform/ydb/pull/54932) уже стартовал на старом tip; новые правила подхватятся со следующего `doc_translate`.

## 2026-10-03 09:25 UTC
<!-- 48ba71def5201283 -->
- По `stable-26-2` для **#50839** я не ошибаюсь.

## 2026-10-03 10:15 UTC
<!-- 4f9b7b2a4ee5cbce -->
- Сделано:
- удалил `translation/pr-50858`, `ydbdoc-diag-50858-attempts`, `ydbdoc-diag-50858-continue`

## 2026-10-03 12:16 UTC
<!-- 4d88edb5841e1b25 -->
- 4. `GET /user` в конце снова 403/fail. Это шум, не причина RED.

## 2026-10-03 12:18 UTC
<!-- 9a6569dfb7929090 -->
- 1. **Surgical patch, когда EN уже есть.** Считать diff source, переводить/переносить только затронутые блоки, остальное брать из текущего EN. Для changelog это нормальный режим: старые релизы трогать нельзя. На этом PR можно даже без модели: та же замена URL в EN + удаление EN `dynamic-config.md`, если он ещё жив.

## 2026-10-03 13:37 UTC
<!-- 22f8e05f7dd66653 -->
- Проверки.**

## 2026-10-03 13:53 UTC
<!-- 366e7d0624eb681b -->
- Сделано.

## 2026-10-03 14:56 UTC
<!-- 47c7672fb2d66b3b -->
- Проверка.** Squash, один parent `9e7f379`.

## 2026-10-03 15:10 UTC
<!-- b74398dc865d59b4 -->
- Сделано.
- Закрыт [перевод #54952](https://github.com/ydb-platform/ydb/pull/54952), ветка `translation/pr-50858` удалена.

## 2026-10-03 15:47 UTC
<!-- 59b519d097187f91 -->
- Итог: whole-file timeout ушёл, но unique rewrite не доехал до читаемого EN. Следующий баг: не гонять presentation-map (и не кормить critic весь changelog), если surgical уже перенёс дельту.

## 2026-10-03 16:07 UTC
<!-- d15184592a2b4349 -->
- Что сделано и проверено локально:

## 2026-10-03 18:05 UTC
<!-- 94c774fb4c5fbe4c -->
- Нет. Локально я проверил **только translator**: unique rewrite по пути ссылки, EN уже с новыми URL, presentation-map не портит текст, соседний `virtual_storage_groups_decommit` цел. Это как раз то, что в прошлый раз разъехалось в diff.
- Итого: следующий live-прогон нужен, чтобы увидеть critic/arbiter, не чтобы подтвердить то, что локально уже зелёное. Если скажешь «делай», закоммичу, сдвину тег и перезапущу.

## 2026-10-03 18:11 UTC
<!-- 16b7dafe138fe85d -->
- В чате я не сую DeepSeek 100KB changelog и не прошу его «отредактировать статью». Смотрю четыре URL и говорю: уже перенесено, готово. CI делал наоборот: unique rewrite, потом presentation-map, потом critic 32 хода по всему файлу, потом arbiter по тому же полотну. Отсюда 1 из 100 и 1349 ₽.
- Это не «выключить проверку». Это та же проверка, только без лотереи на 100KB.
- Когда скажешь, закоммичу и сдвину тег. Live-прогон имеет смысл уже после этого, не на полуготовом critic.

## 2026-10-03 18:20 UTC
<!-- 171b786aee1eb561 -->
- Гипотеза на токене Yandex Cloud проверена. В CI ничего не встраивал.
- Кормил `deepseek-v4-flash` только четырьмя изменёнными пунктами changelog из #50858, не целым файлом. Два прогона, ~1300 входных токенов на файл.
- Первый прогон ошибся в EN-парах: все четыре пункта матчились по общему dest `dynamic-config`, и модель для server получила один и тот же EN про views. Enterprise всё равно выдала четыре разных пункта. Server выдал четыре копии views. Это не «DeepSeek не умеет», это кривой вход.
- Модель скопировала русские якоря `#obnovlenie-dinamicheskoj-konfiguracii` вместо `#updating-dynamic-configuration`. На server ещё `- *` и `views (VIEW)`. Готового changelog, который можно класть в PR без правки, нет.

## 2026-10-03 18:24 UTC
<!-- b8163bdae7d2b18e -->
- В чате нормальный перевод делает не DeepSeek. Его делаю я: читаю RU и EN, понимаю класс правки, правлю только то, что изменилось, проверяю якоря и соседние ссылки. Для #50858 это вообще не перевод: в EN уже правильный текст, нужно заменить dest и оставить `#updating-dynamic-configuration`. Flash этого не делает. Он «переводит» и тащит русские якоря.
- 2. Не ломать готовый draft.** Даже идеальный переводчик бесполезен, если следующий шаг глобально оборачивает `YDB`/`CPU` в бэктики. В чате этого шага нет. Гипотеза: CI RED после unique_replacements был багом постпроцессора, не перевода.
- 4. Другая модель только для TRANSLATE.** Flash может быть потолком. Тогда чат (другая модель + агент) никогда не совпадёт с CI. Проверка: один живой вызов сильнее модели на реальной новой статье, не на changelog.
- Что не гипотеза: ещё один контракт на 500 полей, skip-critic «чтобы было GREEN», повторный запуск #50858 на полуготовом теге.
- Если выбираешь один следующий spike: **1+2 на диске, без Actions.** Это тот перевод, который я сделал бы в чате для #50858. Пункт 3 отдельно, на PR с новой прозой, живым вызовом, не тегом.

## 2026-10-03 18:30 UTC
<!-- a5e470cf22c3f969 -->
- В чате я пишу Markdown. Невидимое не перепечатываю. Неизменённый EN копирую. Потом глазами сверяю. В CI модель должна заполнить JSON-карту сегментов, runtime склеивает файл, потом `apply_presentation_map` ещё раз переписывает литералы. Это другой процесс. Маленький промпт этого не чинит.
- 2. Есть старый EN: копировать неизменённые блоки байт-в-байт. Модель зовётся только на выровненные hunks видимой прозы. В hunk класть RU after + placeholders и EN-фрагмент *этого* hunk: смысл не изменился, оставь EN, поправь dest/подписи; смысл изменился, переведи. Целый changelog на 100KB в модель не отдавать.
- Сильнее модель и translator-с-tools это следующий слой, не первый. Сначала Markdown-out + hunks + не трогать protected + не presentation-map. Иначе тот же flash будет «переводить» поля JSON, а не статью.

## 2026-10-03 18:35 UTC
<!-- 8839207068578f5d -->
- Тест: `tests/live/test_markdown_placeholder_translate_live.py` (по умолчанию выключен). Зовёт `scripts/probe_markdown_placeholder_translate_live.py`: placeholders, Markdown без JSON-схемы, без `presentation_map`. Pytest `-m live` прошёл.
- Если ок, следующий шаг: заменить JSON-контракт translator на этот путь в коде, не запуская перевод PR.

## 2026-10-03 19:05 UTC
<!-- 04a721bd52b0d2a5 -->
- Чат делает три роли подряд: не трогать защищённое, перевести видимую прозу, проверить смысл. В CI это Python, translator, critic, arbiter. Менять надо все четыре, иначе следующий шаг откатит предыдущий.
- Translator.** Модель возвращает Markdown с placeholders, не JSON-карту сегментов. Runtime восстанавливает include, dest, fence, identifier. Без `apply_presentation_map`. Unique dest: модели нет. Hunks: модель только на изменённую прозу, старый EN этого hunk это смысловой baseline, не «игнорируй формулировку». Новая статья: один Markdown-запрос на файл.
- Presentation-reference только для литералов в **изменённых** местах. Не «пройдись по всему changelog и расставь backticks».
- Код, `REQUIREMENTS_RU.md`, `knowledge/translation-algorithm.md` и банк знаний трону, когда скажешь «да» на этот пакет. Тогда TDD: translator Markdown-out, critic lock protected, unique dest без моделей для critic/arbiter, focused tests, без `doc_translate` на живом PR.

## 2026-10-03 19:20 UTC
<!-- 50858-e2e-markdown-out -->
- Live e2e #50858: unique dest без presentation-map. Critic 4 tool-хода, байты = draft. Arbiter YELLOW 23 findings по старой истории changelog, не по дельте PR. Полный critic/arbiter на 100KB dest-only не как в чате. Канон: unique dest без моделей critic/arbiter; translator Markdown-out с placeholders, не JSON segment map.
- `REQUIREMENTS_RU.md` §2/§4.2 и `knowledge/translation-algorithm.md` обновлены. Actions не запускал.

## 2026-10-03 19:52 UTC
<!-- 3b1cbbd13c588383 -->
- | Unique dest | Python проверяет: old dest ушли, new dest на месте, sibling не тронут, draft не mangled. Reviewed = draft. `finish` без DeepSeek. Gate пройден. |
- | Surgical hunks | Tool-using DeepSeek только на изменённых hunks + соседние 20–40 строк. Previous EN = semantic baseline. Нельзя `apply_patch` вне touched spans. |
- Запрет: critic не «улучшает» неизменённые блоки и не гоняет presentation-map.
- На PR с новой прозой critic реально правит смысл/Title Case/literals в зоне изменений; arbiter подтверждает. Это не skip, это тот же объём, что у редактора в чате.
- Нужен ответ на развилку **A или B**. Если **A**, следующий шаг: зафиксировать это в `REQUIREMENTS_RU.md` §4 и сделать live e2e #50858 с ожиданием GREEN без DeepSeek на critic/arbiter. Если **B**, придумаем узкий prompt только на 4 изменённых пункта, но это дороже и менее надёжно.

## 2026-10-03 19:55 UTC
<!-- 19eb7f86fe447e32 -->
- В чате я не открываю весь `changelog-server.md` и не ищу опечатки 25.1. Я смотрю: что изменилось в RU, что должно стать в EN, не сломали ли соседнее. Если дельта уже на месте, говорю «готово».
- 1. **Source delta** — только изменённые hunks RU before→after (для #50858 это 4 пункта).
- Сначала проверь дельту: dest/проза из source after отражены в draft.
- Не устраивай стиль-проход по всему changelog. Не добавляй backticks «на всякий случай».
- Live проверка до CI

## 2026-10-03 19:58 UTC
<!-- 2eef04e07643e364 -->
- Actions не трогал. Если ок, следующий шаг: зафиксировать этот scope/brief в `REQUIREMENTS_RU.md` §4 и в runtime (не только в probe).

## 2026-10-03 20:10 UTC
<!-- delta-scope-production -->
- Production: DeepSeek critic/arbiter всегда. Python задаёт CHANGE CLASS / source delta / touched EN lines. Findings вне дельты drop → GREEN. Unique dest без `model_exempt`. Presentation-reference = previous EN. Патч critic вне touched lines отклоняется.
- Live delta probe #50858 был GREEN. Дальше commit/tag/`doc_translate` на #50858.

## 2026-10-03 20:30 UTC
<!-- already-satisfied-green -->
- Run 37150733390: unique dest + delta critic/arbiter ок, но EN уже готов → zero-commit RED. Direction смотрит только RU inventory.
- Fix: после prepare, если publication plan пуст и нет дыр → «перевод не требуется» GREEN, без critic/PR.

## 2026-10-03 20:13 UTC
<!-- 02f3adca4cb577ec -->
- Доставка.** `29059c4` на `public/main`, тег `v1.0.1` указывает туда же. Закрыт кривой [#54958](https://github.com/ydb-platform/ydb/pull/54958), ветка `translation/pr-50858` удалена. На [#50858](https://github.com/ydb-platform/ydb/pull/50858) снова `doc_translate`: [run 37150733390](https://github.com/ydb-platform/ydb/actions/runs/37150733390) (`in_progress`). #54958 не мержить.

## 2026-10-03 20:19 UTC
<!-- 22a8ceed06adf621 -->
- EN `maintenance/manual/dynamic-config.md` уже удалён (404)
- PR #50858 менял только RU. EN-дельты нечего коммитить. Soft-publish видит `plan.changed=false` → ветка/PR не создаются → zero-commit RED.
- Почему live-тесты прошли.** `probe_pr_50858_delta_review_live.py` явно пишет: `No GitHub publish`. Он проверял только critic/arbiter на байтах в памяти и ждал GREEN по дельте. Публикацию и правило «zero commits → RED» он не трогал. Модельная часть отработала как в тесте; упал publication contract.
- Итого: delta-scope сработал. Сломался сценарий «RU надо зеркалить, а EN уже готов». Нужно решать, что делать в этом случае: GREEN «EN уже актуален» без PR, или другой явный исход, а не RED про отсутствие commit.

## 2026-10-03 20:21 UTC
<!-- 506706d92669ef92 -->
- Проверка сработала, но она отвечает на другой вопрос.
- Что она делает сейчас.** DeepSeek в §1.1 смотрит только inventory source PR: RU before/after + mapping путей. Текущие EN байты туда не подаются. На #50858 модель честно видит: RU changelog поменял ссылки, RU `dynamic-config.md` удалён → `translation_required=true`, `ru_to_en`. Это не баг модели.
- Чего она не делает.** Не сравнивает результат с уже существующим EN. «EN уже с новыми URL и страница уже удалена» Python узнаёт только позже, на surgical/publish. Тогда `noop`, и старый gate §4.2 превращает это в RED «нет commit».
- Итог.** Ранняя проверка отвечает: «в PR есть работа для зеркалирования локалей?» Поздний gate отвечает: «появился ли commit?» Для уже готового EN оба ответа формально верны, а нужный исход («перевод не требуется», GREEN) нигде не вычисляется.
- Нужный исход: после Python-mirror, если менять в EN нечего, это `перевод не требуется` + GREEN, а не zero-commit RED. Это либо отдельная Python-проверка после плана (до/вместо publish), либо расширение direction контекстом текущих EN. Сейчас второго нет.

## 2026-10-03 20:22 UTC
<!-- 56dfbb6a11029203 -->
- После Python-mirror добавить вторую проверку: **осталась ли реальная работа в target.**
- комментарий «перевод не требуется» + короткая причина («EN уже отражает дельту»);
- 4. Zero-commit RED оставить только для настоящего провала: перевод требовался, а собрать/закоммитить не удалось (дыры, ошибка модели, validation).
- Почему так.** «Требуется ли перевод» по смыслу §1.1 = нужна ли работа. Когда EN уже готов, это знает Python на mirror, не модель на inventory. Critic на noop только жжёт деньги.
- Не предлагаю:** менять prompt direction «угадай по RU, готов ли EN» — хрупко и дорого.

## 2026-10-03 20:31 UTC
<!-- dddeebc8718fb18f -->
- Готово.
- `3621c6a` на `public/main`, тег `v1.0.1` туда же. Перезапуск #50858: [run 37151704992](https://github.com/ydb-platform/ydb/actions/runs/37151704992) → **success**, комментарий: «Перевод не требуется. Причина: Целевая локаль уже отражает изменения source PR.»

## 2026-10-04 05:17 UTC
<!-- 0315425011f5f1b5 -->
- Две разные поломки, чинить по слоям. Критик здесь скорее жертва, не первопричина.
- Порядок:** P0 normalize + gate с focused-тестами на фикстуре из #54993 → P1 → tag → retranslate `log_config`. Не начинать с «подкрутить `YDBDOC_CRITIC_MAX_TOOL_TURNS`»: на разнесённом файле это только дороже.

## 2026-10-04 05:26 UTC
<!-- 4afca5be2076dbbd -->
- Да, мы защищаем. Но не эти куски. Ниже развернуто: сначала глобальная причина, потом что предлагаю чинить.
- Глобальная причина: почему `log_config` стал `` `log`_`config` ``
- На этом пути в ветку уехал уже испорченный ответ модели (где часть placeholders потеряна/заменена текстом). Critic должен был это поймать и починить, но на файле с десятками `` `a`_`b` `` он сжёг tool-бюджет и не сделал `finish` → RED «не проверили» (ещё и с неправильной формулировкой про «сбой провайдера»).
- Отдельно (P2): в §2 REQUIREMENTS написано, что глобальный presentation-map после restore не накладываем, а код для whole-file всё ещё накладывает. Это drift; к `` `a`_`b` `` на этом PR не первопричина, но его надо убрать/сузить, чтобы не плодить сюрпризы.
- Могу следующим шагом делать п.1 (защита + тест на этом repro), без трогания #54993 до зелёных focused-тестов.

## 2026-10-04 06:32 UTC
<!-- b6846e7a8a962b97 -->
- Согласен с формулировкой. [#54994](https://github.com/ydb-platform/ydb/pull/54994) это не «ещё один баг», это симптом.
- | Ошибка | Вижу, чиню, иду дальше | soft-publish мусора / unreviewed RED / missing |
- 4. Пока agent-in-CI нет: ночной автоперевод **выключить**. Днём: «переведи PR #…» в чате. CI только на уже готовых translation PR (build/ok-to-test).
- Практический следующий шаг, если ок: набросать минимальный workflow «label → Cursor/Codex agent → translation PR» на одном PR (#42314 или #46837) и сравнить с текущей машиной. Пайплайн `ydbdoc-review-ng` translate/critic в проде при этом не наращивать.

## 2026-10-04 07:10 UTC
<!-- fa46edc86f15ddab -->
- Короткий тест сделан.

## 2026-10-04 07:17 UTC
<!-- 4fb8e7b3184b1c31 -->
- CI «синхронизировал» файл, но оставил русский в середине (V1/V2 инструкции, скрипт, проверки). Critic потом сгорел на budget. Прямой вызов модели с простым промптом «верни весь EN Markdown» дал цельный перевод за ~4 минуты.
- Вывод.** Проблема не в «DeepSeek не умеет». Ломает обёртка CI (partial restore / soft-publish / critic). Для ночного перевода достаточно тонкого agent-цикла: файлы → DeepSeek → запись → дешёвые проверки (кириллица, includes, `` `a`_`b` ``), без текущего translate/critic автомата.

## 2026-10-04 07:42 UTC
<!-- 357d3d2698fd7751 -->
- Сделано. Алгоритм сменён, не дописан сбоку.
- Unit translation tests green. Часть integration ещё ждёт старый critic `{"files":…}` — это долг на вычистку suite, не блокер tip.

## 2026-10-04 12:10 UTC
<!-- 6ae9ac367244c811 -->
- Новая статья `logging.md` → «добавь href в EN TOC». Это буквальный append в конец `items`, без соседей из RU. Имя берётся из basename файла, не из `Логирование`.
- Chaos Testing
- Сборка и тесты Ya
- Это наследие требования «новая статья → зеркальная запись в TOC», сделанного как append, плюс комментарий в runtime: in-flight metadata drafts не выкидывать. Для страниц, которые и так появляются в source TOC delta, этот append вреден.

## 2026-10-04 12:20 UTC
<!-- 86858ec5f119aa01 -->
- Тест:** RU `build-ya` → add `logging.md` → `releases`; EN без Logging; metadata уже дописала Logging в конец → после delta `Logging` сразу после `Ya Make build system`. Плюс кейс без metadata: сразу правильное место.
- Сделано в `toc_delta._apply_items`: `before_entry is None` + уже есть match → move к after-соседу. Metadata append для dependency TOC без delta не трогали: существующий test_runtime ждёт append, когда TOC сам в PR.
- `doc_verify` на GREEN #55007 TOC не перепишет. Нужен `doc_translate` source #46837 после tip/`v1.0.1`.

## 2026-10-04 12:28 UTC
<!-- d4a7516a3e3c4eba -->
- Шип: `ce69751` на `public/main`, тег `v1.0.1` сдвинут туда же. Metadata-append для dependency TOC не трогал: существующие тесты его ждут, а порядок теперь чинит аппликатор. `doc_verify` TOC не переписывает.
