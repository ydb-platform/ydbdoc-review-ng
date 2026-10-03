# Канонические требования к переводу документации

Единственный источник действующих требований. Исторические планы и отчёты
этот контракт не расширяют.

## 0. Простая модель системы

1. Техпис ставит label на PR.
2. Runtime берёт immutable snapshot source PR, при необходимости дотягивает
   зависимости без target-перевода, переводит файлы и публикует translation branch.
3. Critic — обязательный tool-using gate (§4.1): in-memory workspace чанка,
   tools `read` / `grep` / `apply_patch` / `finish`, mandatory re-read после
   каждого успешного patch. Reviewed bytes = runtime-applied patches в ту же
   ветку. Whole-file JSON `{"files": …}` не primary path.
4. Arbiter независимо смотрит окончательный результат и публикует
   `GREEN` / `YELLOW` / `RED`.
5. Build и CI не участвуют в semantic verdict.

Режимы:

| Режим | Label | Суть |
|---|---|---|
| `doc_translate` | на source PR | Новый перевод. Удаляет старую translation branch этого PR и создаёт чистую. |
| `doc_verify` | на translation PR | Без translator. Critic правит текущую ветку, arbiter заново ставит verdict. |
| `doc_continue` | на source или translation PR | Продолжение checkpoint с комментарием `/ydbdoc continue …` как operator context. |

Запуск только от `YDBDOC_ALLOWED_ACTORS`. После принятия label снимается.
Отказ до checkout непроверенного кода, model calls и GitHub mutations, но job
audit record и terminal error в YDB пишутся всегда.

## 1. Направление и scope

### 1.1 Простая проверка (один DeepSeek-вызов)

Runtime сам собирает authoritative inventory: путь, Git-операция
`add/modify/delete/rename`, содержимое text before/after, manifest binary.

Модель **не** назначает действия файлам. Она отвечает только:

- нужен ли перевод;
- направление `ru_to_en` или `en_to_ru`;
- краткая причина.

JSON: `translation_required`, `direction`, `reason`. Одна повторная попытка при
provider error / невалидном JSON. Вторая неудача → job error + комментарий в PR.

Если `translation_required=false` — translation PR не создаётся, в source PR
один комментарий «перевод не требуется» и причина. Тихий no-op запрещён.

Если направление надёжно не определено — checkpoint `direction_undetermined`,
комментарий с просьбой `/ydbdoc continue`.

### 1.2 Действия по файлам (только Python)

После направления Python зеркалит inventory:

- Markdown/YFM source-locale страница → whole-file перевод target;
- delete → удалить парный target;
- rename → зеркально переименовать target; если содержимое ещё изменилось → перевести;
- locale-relative resource/binary → copy/delete/rename без модели;
- TOC → особый путь §3;
- прочее вне locale mapping → без молчаливой потери: явный no-op или ошибка.

Существующий target **опционален**. Если есть — передаётся translator/critic
только как formatting/presentation reference (inline-code, escapes), не как
semantic baseline. Если нет (новый файл) — опираемся на identifier atoms,
style rules и нормализацию presentation в critic.

### 1.3 Дотягивание зависимостей

Для каждой переводимой статьи рекурсивно обходятся внутренние Markdown-ссылки.

- Source B есть, симметричного target нет → B в frozen group, переводится целиком.
- Target B уже есть → рекурсия на B останавливается, B не в review scope.
- Ссылка на `glossary.md` без точного target-anchor → source glossary в scope,
  целиком переводится. Если anchor всё равно нет — diagnostic, не publication gate.
- Для обычных anchors имена RU/EN могут отличаться; scope из-за этого не растёт.
- Новая target-статья → зеркальная достижимая запись в target TOC.
- Changelog и обычные статьи — одни правила.

Лимиты (GitHub Actions variables):

- `YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE` — статья + её dependency group;
- `YDBDOC_MAX_SOURCE_CHARACTERS` — размер **одного** source-файла.

Превышение → job error до model translate/critic/arbiter, один комментарий в
source PR с именем лимита. Для старого слитого PR читается tip base на
зафиксированном SHA, не двигающийся HEAD.

## 2. Перевод документа

Единица перевода — целый Markdown/YFM файл одним translator request.
Внутридокументного chunking нет. Лимит размера — только
`YDBDOC_MAX_SOURCE_CHARACTERS`.

Переводимы: проза, заголовки, списки, таблицы, подписи ссылок, `alt`,
front matter `title`/`description`, заголовки YFM note/cut/tab, комментарии
в поддерживаемых fenced code (§2.1).

Перед вызовом непрозрачные фрагменты → placeholders. URL в link/image:
подпись видна, destination = URL-token. Markdown-синтаксис модели виден.
Защищены: URL path/query, **identifier atoms** (целый `BS_CONTROLLER` /
`CREATE_FAILED` / `POOL_NAME`; CamelCase product names вроде `BlobDepot` /
`LogoBlob`; не рвать на bare ESCAPE `\_` внутри),
templates, inline code, код вне комментариев, Mermaid, include, прочий
front matter, technical HTML. Если old target есть — Python строит
presentation map (какие атомы / CLI flags / short ALLCAPS states /
colon-form tokens были в backticks / без escapes) и накладывает
её на draft после restore; если нет — apply no-op.

Внутренние YDB URL: только `/docs/ru/` ↔ `/docs/en/`. Для `glossary.md`
fragment ищется в реальном target glossary; иначе fail-open diagnostic.
Обычная внешняя ссылка — exact source URL. Исключение: `*.wikipedia.org/wiki/...`
без query/fragment → MediaWiki `langlinks`; нет соответствия → source URL,
arbiter может поставить YELLOW.

Модель возвращает JSON-карту segment IDs без placeholders. Runtime вставляет
protected fragments из source (плюс locale/Wikipedia/glossary rules). Одна
техническая коррекция при ответе, из которого нельзя собрать UTF-8 файл.
Собранный UTF-8 файл публикуется как **draft** (технический soft-publish /
diagnostics). Markdown/YFM/links/anchors/protected diagnostics не блокируют
draft-публикацию, но draft **не** является reader-facing product success.

Глоссарий в translator: все релевантные парные секции текущего файла, без
лимита числа/размера; при равенстве — порядок по anchor. Это контекст, не
текст для вставки.

Existing target в prompt перевода передаётся только как presentation
reference (тег presentation-reference), когда файл уже существовал.

### 2.1 Комментарии в code fence

Лексический scanner, не полный parser языка:

| Fence | Маркеры |
|---|---|
| C++, Java, .NET, JavaScript | `//`, `/* ... */` |
| Python, Bash, YAML | `#` |
| HTML | `<!-- ... -->` |

Строковые литералы учитываются достаточно, чтобы маркер внутри строки не
считался комментарием. Иной язык / `text` без шаблона «синтаксис / пустая
строка / пояснение» — весь fence защищён.

### 2.2 Source echo (RU→EN)

Непрерывный фрагмент ≥32 русских букв после нормализации пробелов в тексте,
видимом модели → одна коррекция translator. Если осталось — публикуем,
diagnostic для critic/arbiter.

## 3. TOC (особый путь, владеет Python)

Не whole-file перевод TOC.

1. Python парсит source TOC до и после PR, считает структурную дельту,
   применяет к полному текущему target TOC: add/delete/rename/change,
   `href`, hierarchy, includes, conditions. Несвязанные target-записи
   сохраняются.
2. Уже отсутствующая в target удаляемая/переименовываемая запись — не ошибка.
   Тот же итоговый `href` — обновить, не дублировать.
3. DeepSeek переводит только новые/изменённые видимые строки (`name`,
   `title`, `label`, …) строгой JSON ID-картой. Одна повторная попытка.
4. Нет target TOC, а source TOC менялся → новый target TOC только с
   добавленными/изменёнными в этом PR записями, не копией всего source TOC.
   Только delete и target TOC нет → новый файл не создаём.
5. Ссылка на отсутствующий target-файл в TOC — diagnostic, не gate.
6. Critic/arbiter получают полный target TOC (или `null`) и source TOC
   before/after. Critic правит TOC только через tool-workspace §4.1
   (`apply_patch` по seeded target TOC), не целым JSON-файлом как primary
   path. Перед принятием patch Python проверяет, что critic не удалил
   target-записи, которых не касалась source-дельта PR. Нарушение делает
   **этот patch** невалидным (как oversized/invalid hunk): одна повторная
   попытка всей critic-сессии §4.1. После второй такой ошибки по TOC —
   исключение из fail→RED: сохраняется TOC, уже построенный Python по
   дельте, и **именно он** идёт в reviewed bytes и дальше в arbiter.
   Transport/protocol/turn-budget ошибки critic по TOC-чанку по-прежнему
   дают unreviewed RED без arbiter на сыром draft.

Покрывается unit/integration tests на дельту, идемпотентность, новый TOC,
diagnostics.

## 4. Critic и arbiter

Модель production: только DeepSeek V4 Flash. Исключение —
`doc_model_probe` (не переводит и не публикует).

`reasoning_effort`: critic `medium`, arbiter `none`, остальные production-роли
`none` (live probes / tip; silent-connection wall на длинном reasoning).
Critic/arbiter получают постоянные инструкции отдельным `developer` message,
а входные файлы, glossary, TOC snapshots, binary manifest и operator context —
`user` message. В конце `user` message повторяется короткий обязательный
checklist: completeness, terminology, technical literals и inline-code,
damaged sentences, TOC и состав файлов чанка.

Контекст: 1 048 576. Для translator/direction `max_tokens` = остаток после
UTF-8 byte-размера полного wire request (1 byte ≈ 1 token). Для critic/arbiter
generation budget дополнительно ограничен (`max_output_tokens` потолки в
коде; иначе silent-connection wall ~270 с → TRANSPORT, http_status=null;
runs 37009373894 / 37027975808). `NON_FINAL` → чанк непроверен, остальные
идут, итог RED.

План rollout tool-using critic: `knowledge/tool-using-critic-plan.md`.
Закрытый цикл arbiter findings → repair **запрещён** (token burn).

### 4.1 Critic

Обязательный quality gate и **единственный** model-editor после draft.
Вход чанка: полные source + **draft** target одной пары frozen group
(статья или TOC), optional presentation-reference (old target по path),
relevant paired glossary без лимита, manifest binary при необходимости,
для TOC — before/after source.

#### Роль и границы

- Critic = tool-using editor: читает, ищет, патчит, **обязательно**
  перечитывает затронутые строки, завершает сессию через tool `finish`.
- Arbiter = judge-only (§4.2). Findings арбитра **не** запускают critic и не
  чинятся автоматически в том же job.
- Translator и Python TOC-delta (§2–§3) не меняются этим контрактом, кроме
  wiring входа/выхода critic и TOC-исключения §3.6.

#### Workspace и tools

Runtime готовит in-memory workspace на чанк:

| Path class | Содержимое | Доступ |
|---|---|---|
| Writable draft target | draft bytes; если обязательный target отсутствует — **seed пустого файла** (0 bytes), не JSON-`null` как единственный способ создания | `read` / `grep` / `apply_patch` |
| Read-only source | полные source bytes пары | только `read` / `grep` |
| Read-only glossary | relevant paired sections | только `read` / `grep` |
| Read-only presentation-reference | old target по path, если был | только `read` / `grep` |
| Read-only TOC snapshots | source TOC before/after (для TOC-чанка) | только `read` / `grep` |

`apply_patch` на read-only path → invalid tool args. Path traversal / escape
из workspace → invalid tool args. Модель ходит в OpenAI-compatible tool loop
(DeepSeek). Primary path — tools, **не** whole-file JSON `{"files": …}`.

Обязательные tools:

| Tool | Назначение |
|---|---|
| `read` | Байты/строки path из workspace (1-based window). |
| `grep` | Поиск pattern в workspace path(s). |
| `apply_patch` | Unified diff / hunks к **одному** writable path; runtime применяет. |
| `finish` | Явное завершение сессии после всех обязательных re-read. |

Другие tools в v1 запрещены. Verdict/findings в ответе critic запрещены.
Finish signal = только tool `finish` (не «финальный JSON без tools»).

#### Протокол хода (enforceable FSM)

Runtime, не prompt, enforced:

1. **Один mutating tool за assistant-turn:** в сообщении с `apply_patch` не
   может быть других tool_calls. Иначе protocol error.
2. Parallel `read`/`grep` в одном turn допустимы только когда нет pending
   re-read и нет `apply_patch` в этом же turn.
3. После **успешного** `apply_patch` runtime вычисляет touched line ranges
   **после** применения hunk и ставит `pending_reread`. Пока
   `pending_reread` не пуст, единственные допустимые tools — `read`, и
   объединение окон `read` должно покрыть каждый pending range. Любой
   `apply_patch` / `grep` / `finish` при непустом pending → protocol error.
4. `finish` при непустом `pending_reread` → protocol error.
5. `finish` на пустом seed без единого успешного patch допустим (no-op
   review): reviewed bytes = seed/draft без изменений.
6. `finish_reason=tool_calls` (или эквивалент provider) при валидных
   `tool_calls` — **нормальный промежуточный** ответ, не `NON_FINAL` и не
   `EMPTY_TEXT` из-за `content=null`.
7. Retry = **полный рестарт** critic-сессии с исходного draft/seed workspace
   и чистой history; не продолжение сломанного multi-turn.

Нарушение protocol → один retry сессии → иначе unreviewed RED (кроме
TOC-исключения §3.6 после двух ошибок защиты target-only записей).

#### Финальные bytes

Reviewed bytes чанка = workspace writable paths после runtime-applied
patches. Текст ассистента и tool args сами по себе не публикуются. Нет
успешного `finish` с валидным workspace → чанк непроверен.

#### Лимиты надёжности

- Max tool turns на чанк (дефолт 12; override env
  `YDBDOC_CRITIC_MAX_TOOL_TURNS` при реализации). Превышение → RED.
- Per-turn `max_output_tokens` для critic-turn ограничен (как сейчас против
  silent-connection wall); history tool-loop не должна заново класть полные
  файлы в каждый user-turn — только начальный context + tool results.
- Patch-not-full-file: runtime отвергает огромные hunks (пороги в плане
  P1; цель — хирургические правки, не пересылка целого файла). Для seed
  0-byte файла создание содержимого через один или несколько patch
  допустимо, пока каждый hunk проходит byte/span cap относительно
  **результата** применения (порог «% файла» для пустого seed не блокирует
  первую осмысленную запись; абсолютный byte cap всё равно действует).
- Чанки строго по **одной** source/target паре (TOC отдельно). Пара не
  делится. Не влезла → unreviewed, остальные идут.
- Полный bilingual glossary.md не кладётся: только relevant paired sections.
- Один retry на transport/503/invalid contract/protocol (см. FSM).
  `NON_FINAL` на промежуточном tool-turn не применяется к `tool_calls`;
  `NON_FINAL`/`length` без валидных tool_calls или на `finish`-turn → как
  сегодня: retry → unreviewed RED.

#### Cutover и rollback

До P0 probe green + P1 suite tip может ещё исполнять one-shot JSON critic
(факт кода). Это не второй канон и не production dual-path.
После cutover silent fallback на whole-file `{"files": …}` **запрещён**.
Env-flag «tools on/off» в production **не** вводим. Rollback = revert
release tip / остановить `doc_translate`, не параллельный JSON-path.

#### Качество правок

Source-разметка не эталон target-разметки. Presentation-reference — только
оформление. Critic нормализует технические литералы: inline-code и снятие
ненужного экранирования (`BS\_CONTROLLER` → `BS_CONTROLLER`). Литерал нельзя
переименовать, перевести, удалить или продублировать. Мягкие Markdown/YFM
diagnostics §2 по-прежнему не gate.

#### Публикация

Успешный чанк → **reviewed** commit/push workspace bytes. Первый успех может
создать branch/PR, если translator опубликовал только draft. Ошибка после
retry → unreviewed RED; arbiter по этим путям **не** вызывается на сыром
draft (TOC-исключение §3.6 — единственное: Python-delta TOC всё же идёт в
arbiter). Critic unavailable ≠ GREEN/YELLOW на raw translator dump.

При нуле текстовых пар → пустой tool-сеанс / no-op `finish`, вызов gate
всё равно есть (как раньше пустой `{"files": {}}`).

### 4.2 Arbiter

Вход: окончательные source/target **только после успешного critic**
(reviewed bytes), тот же glossary/manifest подход, чанки заново по
финальным размерам. Unreviewed пути уже RED и в arbiter не идут.

Ответ только:

```json
{
  "verdict": "GREEN|YELLOW|RED",
  "findings": [
    {
      "target_path": "...",
      "searchable_snippet": "...",
      "reason": "по-русски",
      "expected_correction": "по-русски"
    }
  ]
}
```

GREEN → `findings: []`. YELLOW/RED → ≥1 finding. Missing/unreviewed target →
`searchable_snippet = null`.

Для существующего target модель возвращает точный `searchable_snippet`, но не
считает номер строки. Python ищет snippet в окончательных bytes и вычисляет
`target_line` для публичного отчёта. Snippet должен встречаться ровно один раз.
Отсутствующий или неоднозначный snippet делает непроверенным только этот
finding: остальные валидные findings того же ответа сохраняются, для
затронутого path добавляется непроверенный finding, общий verdict становится
RED. Структурно невалидный JSON/contract по-прежнему делает непроверенным весь
чанк.

Общий verdict = худший по чанкам. Findings всех YELLOW/RED чанков
объединяются (не более 25 в публичном комментарии, остальные — счётчиком).
Любой неуспешный ответ arbiter, включая transport/provider failure или
структурно невалидный contract, означает непроверенный чанк: остальные чанки
продолжают проверяться, общий verdict = RED. Это же правило действует, когда
arbiter chunk единственный. Публичная причина различает context overflow,
transport/provider failure, структурно невалидный ответ и неразрешимый
snippet; сообщение «уменьшите файл» допустимо только для context overflow.

Смысл цветов:

| Verdict | Значение | Job | Checkpoint |
|---|---|---|---|
| GREEN | перевод корректен | успех | закрыт |
| YELLOW | мелкие проблемы (нет EN Wikipedia, термины и т.п.); правится руками за минуты | **успех** | **закрыт** |
| RED | серьёзные ошибки, дыры, непроверяемый файл | неуспех | открыт → `/ydbdoc continue` или ручная правка + `doc_verify` |

YELLOW findings публикуются в QA comment. Автопочинки arbiter findings нет.

Если ни translator, ни critic не создали commit → пустой PR не создаём;
RED-отчёт в source PR; checkpoint с `target_sha=null`.

## 5. Оркестрация

### 5.1 `doc_translate`

1. Job audit → auth → snapshot.
2. Дневной budget gate (§6) до любого model call.
3. Direction call (§1.1) → Python scope (§1.2–1.3).
4. Перевести все страницы; TOC по §3; deterministic ops.
5. Один первоначальный **draft** commit собранных файлов + deterministic ops →
   translation PR (технический soft-publish; diagnostics ≠ product). Частичные
   model-fail → остальные всё равно в draft; failed paths = `null` для critic.
   Нет ни файлов, ни ops → commit пока нет, процесс идёт к critic.
6. Critic по §4.1 (обязательный gate → reviewed commits). Затем arbiter по
   §4.2 только на reviewed bytes. Нет успешного critic → RED/checkpoint,
   не GREEN/YELLOW на сыром dump.
7. QA comment + terminal status (честный цвет).

Новый `doc_translate` всегда удаляет прежнюю remote translation branch этого
source PR и открытые checkpoints старого translation PR, создаёт чистую ветку
от pinned base tip. Заголовок PR: `PR #<n> translation`.

### 5.2 `doc_verify`

Без budget gate и без translator. Текущий translation head + authoritative
source snapshot → полный critic + arbiter по всей группе. Исправления critic
пушатся в ту же ветку. Costs учитываются в дневной сумме следующего
`doc_translate`.

### 5.3 `doc_continue`

Только живой checkpoint (14 дней), pinned source/base SHA, комментарий
разрешённого автора с первой строкой `/ydbdoc continue` строго до label.

Continuable: `direction_undetermined`, незавершённый перевод (`pending_paths`),
RED arbiter. YELLOW checkpoint не открывает.

Operator context — отдельный блок инструкций, не часть authoritative Markdown,
не должен попасть в candidate.

- Direction success в том же run → сразу scope + translation.
- Сначала `pending_paths`, потом полный critic/arbiter всей группы.
- Candidate только из `target_sha` ветки; содержимое файлов в YDB не хранится.
- Только GREEN/YELLOW закрывают checkpoint. RED → новый checkpoint с тем же
  первоначальным expiry (TTL не продлевается).
- Нет budget gate; costs в дневную сумму следующего `doc_translate`.

State v3: `state_version=3`, `stage` ∈ {direction, translation, review},
`direction`, `scope_sha256`, `target_sha`, `pending_paths`, `review_paths`
(информативно; review всегда полный по группе).

GitHub transport: retry только идемпотентный GET (≤2) на network/429/5xx.
Мутации и прочие 4xx завершают job.

## 6. YDB, TTL, бюджет

YDB: jobs, model attempts (с cost/usage), continuation checkpoints.
TTL текстов 14 дней средствами YDB. Request/response не в публичные логи.

Cost DeepSeek V4 Flash из usage: in 0,3 / cached in 0,075 / out 0,5 руб. за 1000.
Unknown → `NULL`, не ноль.

Дневной budget (`YDBDOC_DAILY_BUDGET_RUB`, Europe/Moscow): перед новым
`doc_translate` сумма известных costs. Если уже ≥ лимита → ошибка без model
calls. Иначе job идёт целиком, даже если сама пересечёт лимит.
`doc_verify` / `doc_continue` gate не имеют.

## 7. Публикация и отчёт

- Репозиторий `ydb-platform/ydb`, base = base source PR.
- Технически собранный UTF-8 может уйти в ветку как **draft** (soft-publish
  diagnostics). Reader-facing product / success job — после **успешного
  critic** (reviewed) и arbiter GREEN/YELLOW. Soft-publish ≠ «перевод готов».
- Один актуальный QA comment в translation PR (или RED в source PR, если PR
  перевода нет): цвет, краткое резюме, cost job (unknown ≠ 0).
- В source PR — один обновляемый комментарий со ссылкой на translation PR.
- SHA, внутренние коды, transcripts — только в YDB audit.

## 8. Тестирование и поставка

- TDD на атомарный функционал; focused tests после задачи; полный suite / Ruff /
  mypy / `git diff --check` один раз перед release.
- Fakes для model/YDB/GitHub. Mutation testing и quota-матрицы без отдельной
  просьбы не делать.
- TOC delta, soft-publish diagnostics, identifier atoms, draft/reviewed gate —
  обязательные witnesses.
- Работа в `main` без feature branches: частые commits + push.
- Два параллельных потока только без пересечения production-файлов.
- При смене требований — переписать на месте, не дублировать старое рядом.
- `scripts/probe_critic_fallback.py` / label `doc_model_probe` — исторический
  probe, не production translate.

Acceptance (минимум): auth; budget; dependency pull A→A1; whole-file translate;
identifier atoms; optional presentation map; TOC delta tests; critic
tool-workspace + patches + mandatory re-read → reviewed push; critic fail →
RED; arbiter GREEN/YELLOW/RED только на reviewed, без repair-loop; YELLOW не
открывает checkpoint; continue с operator context; empty PR не создаётся при
нуле commits. Rollout plan: `knowledge/tool-using-critic-plan.md`.
