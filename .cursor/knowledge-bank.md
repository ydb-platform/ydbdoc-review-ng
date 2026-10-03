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
