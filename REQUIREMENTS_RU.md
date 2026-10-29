# Канонические требования к переводу документации

Единственный источник действующих требований. Исторические планы и отчёты
этот контракт не расширяют.

## 0. Простая модель системы

1. Техпис ставит label на PR.
2. Runtime берёт immutable snapshot source PR, при необходимости дотягивает
   зависимости без target-перевода, переводит файлы и публикует translation branch.
3. Перевод: целый source Markdown → один DeepSeek вызов → целый target
   Markdown. Python проверяет техническую целостность (§2.3). Стиль, регистр
   букв и отдельные слова другого языка не блокируют публикацию. Один retry
   при техническом провале, затем файл не публикуется.
4. Tool-using critic (`read`/`grep`/`apply_patch`/`finish`) **снят** с
   production path. Reviewed bytes = draft, прошедший gates.
5. Arbiter независимо смотрит опубликованный результат (дельта source PR) и
   публикует `GREEN` / `YELLOW` / `RED`.
6. Build и CI не участвуют в semantic verdict.

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

### 1.1 Проверка изменений каждой языковой пары

Runtime собирает immutable Git inventory. Классификатор получает изменения
RU и EN, целые затронутые Markdown-блоки и контекст заголовков. Полные
before/after всех файлов и их повторные копии в pairs не передаются.
Для новой страницы передаётся её содержимое в пределах лимита запроса.
Пункт списка передаётся целиком, вместе с вложенными пунктами и продолжениями.
Если полный запрос с резервом ответа не помещается в бюджет токенов,
изменения разбиваются на несколько
запросов с полным покрытием дельт обеих локалей. В каждом запросе явно указан
ограниченный набор изменений для оценки и приложен контекст другой локали.
`no_translation` для пары допустим только при таком решении для всех частей.
Неопределённость частей и противоположные направления сначала агрегируются;
затем применяется правило RU → EN ниже. Направление допустимо только из локали, чья дельта включена
в текущую часть; недопустимый ответ повторяется один раз, затем `conflict`.
Неделимый фрагмент сверх лимита не обрезается. Таблицы передаются целиком,
если помещаются; отдельного лимита 64 KiB нет. Проверяется полный wire request,
включая schema и служебные поля, с резервом 4096 output tokens. Классификатор
учитывает фактически наблюдавшееся окно endpoint 262144 tokens; оценка входа
консервативная (верхняя граница по UTF-8 bytes), не точный tokenizer.

При need_context существующий повтор передаёт полные текущие статьи RU/EN
непрерывным текстом. Если полный запрос не помещается, передаются целые
соответствующие разделы со всеми подразделами. Разделы ищутся отдельно для
каждой дельты по ссылкам, inline-code и общим заголовкам/явным IDs, без
позиционного сопоставления RU/EN. Ненайденный раздел сохраняет need_context;
пустой повтор не отправляется. Разделы не режутся на части.
Подтверждённо отсутствующий counterpart не считается новой страницей.
Правка ссылки в старой статье не требует создания давно отсутствующей
статьи/пассажа другой локали; новые статьи и содержательные изменения
классифицируются отдельно и не пропускаются этим правилом.

Модель сначала формулирует причину, затем решение. Wire поля:
`key`, `reason`, `translation_action`; внутренний action и старые checkpoints
совместимы. Это один вызов классификатора без отдельного проверяющего.
Локализованные ссылки/якоря не обязаны буквально совпадать. Исправление
исходной ссылки не требует перевода, если counterpart уже указывает на
соответствующий раздел. Недостаток доказательств даёт need_context.

Каждая пара получает отдельное решение: `no_translation`, `ru_to_en`,
`en_to_ru`, `need_context`, `conflict`, с краткой причиной. Модель не назначает
произвольные Git-операции. Направление одной пары не распространяется на весь PR.
Разные пары одного PR могут переводиться в разные стороны.

Если изменения обеих локалей уже согласованы, перевод не нужен. Сам факт
изменения обоих файлов недостаточен: частичный перевод требует переноса
недостающих изменений; противоречие требует пояснения пользователя.
`need_context` разрешает одно ограниченное расширение целыми разделами.
Если направление остаётся неопределённым (`need_context` / `conflict`),
выбирается RU → EN при наличии русской статьи и русской дельты в паре.
Это детерминированное правило после агрегации всех частей, без нового вызова
модели. В reason сохраняются исходная причина и отметка fallback.
Однозначные решения модели не меняются. Ошибка API, невалидный ответ,
невалидный исходный текст и непомещающийся неделимый фрагмент не дают
разрешения на перевод. Без русской статьи/дельты сохраняется checkpoint.

Если все пары согласованы и нет другой работы, job успешен: новый комментарий
«Перевод не требуется.» без причины для каждого запуска, без translator и
создания translation PR. Старый комментарий не редактируется даже при той же причине.
Source PR с `<!-- ydbdoc-source-pr: -->` по-прежнему не переводится повторно.
Решения пар сохраняются в checkpoint; continue воспроизводит их без новой
классификации. Подробности: `knowledge/pair-classification.md`.

### 1.2 Действия по файлам (только Python)

Для выбранной пары с изменениями обеих локалей использовать частичный перенос:
source delta + текущий target -> точные непересекающиеся замены уникальных
фрагментов target. Нельзя повторно добавлять уже переведённые вставки,
пропускать недостающие удаления или перезаписывать весь target. Неизменённые
байты сохраняются. Неоднозначный patch после одной повторной попытки
останавливает перевод этого документа без опасного fallback.

После направления Python зеркалит inventory:

- Markdown/YFM source-locale страница → перевод target. Если у пары уже есть
  target **и** известен source before/after (open PR: `base.sha`/`head`;
  merged: first parent merge-commit / merge-commit, не плавающий `base.sha`
  и не текущий tip `main`), runtime сначала пробует
  **surgical update**: перенести только source-delta на существующий target
  (уникальные замены URL/строк без модели и без presentation-map; иначе модель
  переводит только выровненные hunks; привязка hunk к EN — по уникальному
  language-agnostic якорю из `before`: link destination, затем inline code
  `` `...` ``, затем `{#anchor}`; **insert-only** дельта без якоря переводится
  как один hunk и дописывается в конец существующего target; на стыке Python
  гарантирует пустую строку перед ATX-заголовком). Presentation-map после hunk
  не гоняется: существующий EN уже presentation baseline. Если source-delta —
  только удаления, и ни один удалённый hunk не находится в EN (типичный
  RU-only note / experimental warning), Python делает **noop**: публикует
  существующий EN без модели и без whole-file. Если семантическая проверка пары подтвердила согласованность изменений
  обеих локалей, Python пропускает эту пару. Если нет другой работы,
  результат «перевод не требуется», PR не создаётся. Whole-file перевод — fallback, если
  якоря нет / он не уникален в EN, или target/source-before нет;
- delete → удалить парный target;
- rename → зеркально переименовать target; если содержимое ещё изменилось → перевести;
- locale-relative resource/binary → copy/delete/rename без модели;
- TOC → особый путь §3;
- прочее вне locale mapping → без молчаливой потери: явный no-op или ошибка.

Существующий target **опционален**. Для surgical hunks он semantic baseline
неизменённых блоков и presentation-reference только для изменённых фрагментов.
Для whole-file fallback по-прежнему: formatting/presentation reference
(inline-code, escapes), не semantic baseline всего файла. Если target нет
(новый файл) — опираемся на identifier atoms, style rules и нормализацию
presentation в critic.

### 1.3 Дотягивание зависимостей

Для каждой переводимой статьи рекурсивно обходятся внутренние Markdown-ссылки.

- Source B есть, симметричного target нет → B в frozen group, переводится целиком.
- Target B уже есть → рекурсия на B останавливается, B не в review scope.
- Ссылка на `glossary.md`: нет target-файла → glossary в scope как обычная
  missing-target страница. Target-файл есть, а точного anchor нет → diagnostic,
  целый glossary не переводим (thin whole-file на 90KB+ сжигает transport).
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

Единица перевода — Markdown/YFM файл.

- **Unique string replacements (без модели):** если source before/after и
  existing target позволяют перенести дельту уникальными заменами
  (URL/строки) — Python делает это сам. Presentation-map запрещён.
- **Thin whole-file (основной model path):** один DeepSeek request на весь
  файл. Вход = полный source Markdown (+ optional old target как reference).
  Выход = полный target Markdown. Opaque placeholders **не** используются.
  Внутридокументного chunking нет. Лимит размера — только
  `YDBDOC_MAX_SOURCE_CHARACTERS`.
- Surgical hunks с placeholders — legacy path для выровненных правок. Insert-only
  source-delta на существующий target: thin translate **только вставки**, append
  в конец target. Whole-file — если выровнять нельзя.

Переводимы: проза, заголовки, списки, таблицы, подписи ссылок, `alt`,
front matter `title`/`description`, заголовки YFM note/cut/tab, комментарии
в поддерживаемых fenced code (§2.1).

Модель обязана сохранить YFM/Diplodoc markup, `{% include %}`, code fences,
paths, identifiers, flags, templates (`{{ ydb-short-name }}` и т.п.).
Thin unwrap снимает только обёртку всего ответа (` ``` ` / ` ```markdown ` /
` ```md `). Файл, который сам начинается с ` ```bash ` / ` ```text `, это
документ, не обёртка: открывающий fence не трогать.
Python не переписывает регистр и backtick-последовательности ответа, не
склеивает строки. Исключение: перед проверкой собранного изменённого документа
добавляет недостающие пустые строки вокруг ATX-заголовков верхнего уровня
для MD022. Код, HTML, YAML front matter и содержимое контейнеров не трогает.
Качество перевода и возможные искажения оценивает semantic review.

Внутренние YDB URL: только `/docs/ru/` ↔ `/docs/en/`. Для `glossary.md`
fragment ищется в реальном target glossary; иначе fail-open diagnostic.
Обычная внешняя ссылка — exact source URL. Исключение: `*.wikipedia.org/wiki/...`
без query/fragment → MediaWiki `langlinks`; нет соответствия → source URL,
arbiter может поставить YELLOW.

JSON segment map от модели → reject + retry, затем fail.
Файл, не прошедший gates после retry, **не** публикуется (null / дыра → RED).
Наличие кириллицы само по себе не доказывает ошибку перевода.

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

Длинное дословное повторение исходной прозы может вызвать одну корректировку
в путях, использующих проверку source echo. Повторное совпадение остаётся
диагностикой. Имена, идентификаторы, цитаты и примеры на другом языке не
являются причиной отбрасывать весь документ.

### 2.3 Publication gates (Python, техническая целостность)

| Gate | Условие провала |
|---|---|
| `utf8_invalid` | результат не декодируется как UTF-8 |
| `missing_include_target` | настоящий relative `{% include %}` ссылается на отсутствующий файл в результирующем target tree: overlay PR и translation base |

Директивы include внутри примеров кода не являются зависимостями.
Регистр заголовков, пустые строки, число пробелов после маркера списка,
неуказанный язык fence и кириллица не являются publication gates.
Python не склеивает строки prose и не исправляет split-backtick глобально:
это могло менять YAML, переносы Markdown и содержимое примеров кода.
При сборке отдельных chunks/hunks сохраняется необходимая граница блоков.

Новая страница из source TOC (в т.ч. dependency): Python применяет structural TOC delta от source (parent section + соседи), а не плоский append basename в хвост. Кириллические `name` переводятся через TOC string map.

Провал → один retry → иначе файл = null, публичный RED finding. Успех →
файл публикуется как reviewed (tool-critic не вызывается).

## 3. TOC (особый путь, владеет Python)

Не whole-file перевод TOC.

1. Python парсит source TOC до и после PR, считает структурную дельту,
   применяет к полному текущему target TOC: add/delete/rename/change,
   `href`, hierarchy, includes, conditions. Несвязанные target-записи
   сохраняются. Новая в source запись, которая в target уже есть (часто
   хвост после metadata-append), переставляется к соседу из source, а не
   правится на месте.
2. Уже отсутствующая в target удаляемая/переименовываемая запись — не ошибка.
   Тот же итоговый `href` — обновить, не дублировать.
3. DeepSeek переводит только новые/изменённые видимые строки (`name`,
   `title`, `label`, …) строгой JSON ID-картой. Одна повторная попытка.
4. Нет target TOC, а source TOC менялся → новый target TOC только с
   добавленными/изменёнными в этом PR записями, не копией всего source TOC.
   Только delete и target TOC нет → новый файл не создаём.
5. Ссылка на отсутствующий target-файл в TOC — diagnostic, не gate.
6. Arbiter получает полный target TOC (или `null`) и source TOC before/after.
   Target TOC после §3 Python-delta считается reviewed (tool-critic нет).

Покрывается unit/integration tests на дельту, идемпотентность, новый TOC,
diagnostics.

## 4. Quality gate и arbiter

Модель production: только DeepSeek V4 Flash. Исключение —
`doc_model_probe` (не переводит и не публикует).

`reasoning_effort`: arbiter `none`, translator/direction `none`.
Документированный контекст: 1 048 576. Локальная оценка входа использует
UTF-8 byte-размер wire request (1 byte ≈ 1 token). `max_tokens` не превышает
остаток окна, лимит генерации DeepSeek 262144 и меньший лимит роли
(классификатор пары <=4096). Если провайдер сообщает меньший доступный
контекст и фактический размер входа, один повтор может уменьшить только
ответ. Вход молча не обрезается. Закрытый цикл arbiter findings → repair
**запрещён** (token burn).

### 4.1 Quality gate (вместо tool-using critic)

Python publication gates §2.3 проверяют техническую целостность на выходе translator.
Смысл и качество перевода оценивает arbiter.

- Tool-using DeepSeek critic (`read` / `grep` / `apply_patch` / `finish`) **не
  вызывается** в `doc_translate` / `doc_verify` / `doc_continue`.
- Файл, прошедший gates, считается **reviewed** и публикуется.
- Обязательный target = null после translate → unreviewed RED (`missing`),
  arbiter по этому path не зовётся.
- `doc_verify` заново гоняет gates на текущих bytes + arbiter; model-editor
  правок в том же job нет (правки — руками или новый `doc_translate`).

Исторический план tool-critic (`knowledge/tool-using-critic-plan.md`) —
архив, не канон.

### 4.2 Arbiter

Вход: окончательные source/target **после publication gates** (reviewed
bytes), тот же glossary/manifest подход, чанки заново по финальным размерам.
Unreviewed / null пути уже RED и в arbiter не идут. Scope = PR delta +
previous EN. Findings вне touched EN lines Python отбрасывает; пустой набор
→ GREEN. Не судить исторический changelog, который PR не менял.

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

Если после Python-mirror целевая локаль уже отражает дельту source PR
(нет дыр translator, publication plan без изменений) → комментарий
«перевод не требуется» + GREEN; arbiter и translation PR не запускаются.
Это не failure.

Если перевод требовался, но собрать/закоммитить не удалось (дыры,
ошибка модели) → пустой PR не создаём; RED-отчёт в source PR;
checkpoint с `target_sha=null`.

## 5. Оркестрация

### 5.1 `doc_translate`

1. Job audit → auth → snapshot.
2. Дневной budget gate (§6) до любого model call.
3. Direction call (§1.1) → Python scope (§1.2–1.3).
4. Перевести все страницы; TOC по §3; deterministic ops.
4a. Если publication plan пуст и нет translator-дыр → «перевод не требуется»
    + GREEN (target уже зеркалит дельту); дальше не идём.
5. Commit файлов, прошедших publication gates (§2.3), + deterministic ops →
   translation PR. Failed gate / model → path = `null` (дыра), не полу-EN.
   Нет ни файлов, ни ops → commit пока нет.
6. Arbiter по §4.2 на опубликованных (reviewed) bytes. Дыры → RED.
7. QA comment + terminal status (честный цвет).

Новый `doc_translate` всегда удаляет прежнюю remote translation branch этого
source PR и открытые checkpoints старого translation PR, создаёт чистую ветку
от pinned base tip. Заголовок PR: `PR #<n> translation`.

### 5.2 `doc_verify`

Без budget gate и без translator. Текущий translation head + authoritative
source snapshot → publication gates + arbiter по всей группе. Model-editor
правок в verify нет. Costs учитываются в дневной сумме следующего
`doc_translate`.

### 5.3 `doc_continue`

Только живой checkpoint (14 дней), pinned source/base SHA, комментарий
разрешённого автора с первой строкой `/ydbdoc continue` строго до label.

Continuable: `direction_undetermined`, незавершённый перевод (`pending_paths`),
RED arbiter. YELLOW checkpoint не открывает.

Operator context — отдельный блок инструкций, не часть authoritative Markdown,
не должен попасть в candidate.

- Direction success в том же run → сразу scope + translation.
- Сначала `pending_paths`, потом gates + arbiter всей группы.
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

- CI публикует ветку `robots/translation/pr-N`, Git-объекты и PR в `ydb-platform/ydb`.
  Base = base source PR. Source/base и target читаются из той же репы.
  Verify и continue проверяют head repository.
- В ветку уходят только файлы, прошедшие publication gates (§2.3). Невалидный UTF-8
  и отсутствующие настоящие include блокируют публикацию, стиль не блокирует.
  Success job — arbiter GREEN/YELLOW на этих bytes.
- Один актуальный QA comment в translation PR (или RED в source PR, если PR
  перевода нет): цвет, краткое резюме, cost job (unknown ≠ 0).
- В source PR — один обновляемый комментарий со ссылкой на translation PR.
- SHA, внутренние коды, transcripts — только в YDB audit.

## 8. Тестирование и поставка

- TDD на атомарный функционал; focused tests после задачи; полный suite / Ruff /
  mypy / `git diff --check` один раз перед release.
- Fakes для model/YDB/GitHub. Mutation testing и quota-матрицы без отдельной
  просьбы не делать.
- TOC delta, thin translate, publication gates (UTF-8 / missing include), допустимый стиль и
  сохранность примеров кода — обязательные witnesses.
- Работа в `main` без feature branches: частые commits + push.
- Два параллельных потока только без пересечения production-файлов.
- При смене требований — переписать на месте, не дублировать старое рядом.
- `scripts/probe_critic_fallback.py` / label `doc_model_probe` — исторический
  probe, не production translate.

Acceptance (минимум): auth; budget; dependency pull A→A1; thin whole-file
translate; publication gates fail-closed; TOC delta tests; arbiter
GREEN/YELLOW/RED на gated bytes без repair-loop; YELLOW не открывает
checkpoint; continue с operator context; empty PR не создаётся при нуле
commits.
