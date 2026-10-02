# Канонические требования к переводу документации

Единственный источник действующих требований. Исторические планы и отчёты
этот контракт не расширяют.

## 0. Простая модель системы

1. Техпис ставит label на PR.
2. Runtime берёт immutable snapshot source PR, при необходимости дотягивает
   зависимости без target-перевода, переводит файлы и публикует translation branch.
3. Critic получает полные source-файлы группы и полные target-файлы перевода,
   находит проблемы и сразу возвращает исправленные target-файлы. Runtime
   применяет их в ту же ветку.
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

Существующий target не подмешивается в translator prompt и не используется
как baseline формулировок.

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
Защищены: URL path/query, identifiers, templates, inline code, код вне
комментариев, Mermaid, include, прочий front matter, technical HTML.

Внутренние YDB URL: только `/docs/ru/` ↔ `/docs/en/`. Для `glossary.md`
fragment ищется в реальном target glossary; иначе fail-open diagnostic.
Обычная внешняя ссылка — exact source URL. Исключение: `*.wikipedia.org/wiki/...`
без query/fragment → MediaWiki `langlinks`; нет соответствия → source URL,
arbiter может поставить YELLOW.

Модель возвращает JSON-карту segment IDs без placeholders. Runtime вставляет
protected fragments из source (плюс locale/Wikipedia/glossary rules). Одна
техническая коррекция при ответе, из которого нельзя собрать UTF-8 файл.
Собранный файл **всегда публикуется**. Markdown/YFM/links/anchors/protected
diagnostics не блокируют публикацию.

Глоссарий в translator: все релевантные парные секции текущего файла, без
лимита числа/размера; при равенстве — порядок по anchor. Это контекст, не
текст для вставки.

Existing target в prompt перевода не передаётся.

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
   before/after; critic может вернуть полный исправленный TOC.

Покрывается unit/integration tests на дельту, идемпотентность, новый TOC,
diagnostics.

## 4. Critic и arbiter

Модель production: только DeepSeek V4 Flash. Исключение —
`doc_model_probe` (не переводит и не публикует).

Контекст: 1 048 576; `max_tokens` = остаток после UTF-8 byte-размера полного
wire request (1 byte ≈ 1 token для этого расчёта). Будущий response заранее
не оценивается. `NON_FINAL` → чанк непроверен, остальные идут, итог RED.

### 4.1 Critic

Вход: полные source/target пары frozen group (PR + дотянутые зависимости +
изменённые TOC), relevant paired glossary без лимита, manifest binary,
для TOC — before/after source. Отсутствующий обязательный target = JSON
`null` → critic обязан создать полный файл.

Critic возвращает только `{"files": {"path": "complete content", ...}}`.
Findings, verdict, patches запрещены. Каждый запрошенный path ровно один раз.
При нуле текстовых пар → `{"files": {}}`, вызов всё равно есть.

Source-разметка не считается эталоном качества target-разметки. Перед ответом
critic отдельно проверяет все технические литералы и исправляет их presentation:
может добавить/убрать inline-code и удалить ненужное Markdown-экранирование
вроде `BS\_CONTROLLER` → `BS_CONTROLLER`. Сам технический литерал при этом
нельзя переименовать, перевести, удалить или продублировать. Такие исправления
публикуются по общему правилу мягких Markdown/YFM diagnostics §2.

Один вызов, если влезает; иначе чанки по целым файловым парам. Пара не
делится. Не влезла одна пара → непроверена, остальные идут.

Успешный чанк сразу commit/push в translation branch. Первый успех может
создать branch/PR, если translator ничего не опубликовал. Ошибка чанка после
одного retry не откатывает другие; его файлы идут к arbiter как есть.
Падение critic само по себе не делает RED, если arbiter потом GREEN.

### 4.2 Arbiter

Вход: окончательные source/target после critic, тот же glossary/manifest
подход, чанки заново по финальным размерам.

Ответ только:

```json
{
  "verdict": "GREEN|YELLOW|RED",
  "findings": [
    {
      "target_path": "...",
      "target_line": 1,
      "searchable_snippet": "...",
      "reason": "по-русски",
      "expected_correction": "по-русски"
    }
  ]
}
```

GREEN → `findings: []`. YELLOW/RED → ≥1 finding. Missing/unreviewed target →
`target_line` и `searchable_snippet` = `null`.

Общий verdict = худший по чанкам. Findings всех YELLOW/RED чанков
объединяются (не более 25 в публичном комментарии, остальные — счётчиком).
Любой неуспешный ответ arbiter, включая transport/provider failure и неверную
пару `target_line` / `searchable_snippet`, означает непроверенный чанк:
остальные чанки продолжают проверяться, общий verdict = RED, а невалидные
findings не публикуются. Это же правило действует, когда arbiter chunk
единственный.

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
5. Один первоначальный commit всех собранных файлов + deterministic ops →
   translation PR. Частичные model-fail → остальные всё равно публикуются;
   failed paths = `null` для critic. Нет ни файлов, ни ops → commit пока нет,
   процесс идёт к critic.
6. Critic по §4.1, затем arbiter по §4.2.
7. QA comment + terminal status.

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
- Публикуем любой технически собранный UTF-8 файл.
- Один актуальный QA comment в translation PR (или RED в source PR, если PR
  перевода нет): цвет, краткое резюме, cost job (unknown ≠ 0).
- В source PR — один обновляемый комментарий со ссылкой на translation PR.
- SHA, внутренние коды, transcripts — только в YDB audit.

## 8. Тестирование и поставка

- TDD на атомарный функционал; focused tests после задачи; полный suite / Ruff /
  mypy / `git diff --check` один раз перед release.
- Fakes для model/YDB/GitHub. Mutation testing и quota-матрицы без отдельной
  просьбы не делать.
- TOC delta и soft-publish — обязательные witnesses.
- Работа в `main` без feature branches: частые commits + push.
- Два параллельных потока только без пересечения production-файлов.
- При смене требований — переписать на месте, не дублировать старое рядом.
- `scripts/probe_critic_fallback.py` / label `doc_model_probe` — исторический
  probe, не production translate.

Acceptance (минимум): auth; budget; dependency pull A→A1; whole-file translate;
TOC delta tests; critic apply+push; arbiter GREEN/YELLOW/RED; YELLOW не открывает
checkpoint; continue с operator context; empty PR не создаётся при нуле commits.
