# Алгоритм перевода и утверждённый полный PR review

Whole-file/source-only translation сохраняется. Полный PR review утверждён
2026-09-30, но ещё не реализован и не provider-validated на baseline `2a1c268`.

## Целый документ и protected fragments

Единица перевода по умолчанию, целый source Markdown/YFM документ. Parser
находит только непрозрачные source fragments: URL, paths, anchors, identifiers,
templates, inline code, нетранслируемый код и служебные конструкции. Markdown-
разметка, включая `**`, заголовки, списки и `|` в таблицах, остаётся видимой
модели. Они
заменяются уникальными placeholders, а Markdown/YFM-разметка и вся переводимая
проза остаются видимы модели. Модель получает явное направление RU→EN или EN→RU
и возвращает только целый переведённый Markdown.

Переводчик всегда получает только authoritative source и возвращает полный
переведённый Markdown. Существующий target используется только для scope,
проверки ссылок и публикации, но не передаётся модели и не используется
сборщиком как источник текста. В каждой Markdown link/image-конструкции подпись
остаётся видимой, а destination заменяется одним URL-token:
`[label]([[YDBDOC_URL_0001]])`. Token хранит source destination и восстанавливается
сборщиком после перевода. Модель переводит label, но не может изменить URL;
наличие, ровно одно вхождение и порядок URL-token проверяются до восстановления.
Внутренний YDB URL меняет только `/docs/ru/` ↔ `/docs/en/`; относительный path и
query сохраняются. Для обычной страницы fragment не участвует в сравнении
симметрии RU и EN: разные target-local anchors допустимы. Для ссылки на
`glossary.md` проверяется именно source fragment. Если такой anchor отсутствует в
target glossary, в scope добавляется source glossary, чтобы его полный перевод
создал соответствующий target anchor. Произвольного угадывания нет. Обычный внешний URL
возвращается из source.
Только для чистой статьи Wikipedia без query/fragment официальный MediaWiki
`langlinks` может вернуть URL target-языка. Отсутствие соответствия или сбой API
оставляет source Wikipedia URL и не создаёт RED.

Dependency closure проверяет внутренние Markdown-ссылки каждого переводимого
source-документа, в том числе документа с существующим target. Если linked
source-статья существует, а симметричного target по тому же locale-relative path
нет, она рекурсивно добавляется в scope, переводится целиком и добавляется в
симметричный target TOC, если достижима из source TOC. Старый target не меняет
destination и не поставляет текст для новой статьи.
Если source-anchor существует, а в парном target-глоссарии точного anchor ещё
нет, в scope добавляется source glossary, а target строится из полного source
перевода. Обычный linked документ из-за различия anchor scope не расширяет.
Исторические changelog-файлы используют те же
правила dependency closure: существующая source-статья по ссылке добавляется в
scope вместе с парным target, а для нового target готовится симметричная TOC-
запись. Поэтому финальная проверка не получает ссылку на непереведённую
статью, исключённую из scope.

Лимиты scope проверяются независимо: число dependency-файлов и объём исходного
текста дают разные диагностические ошибки. При превышении job останавливается до
вызовов модели и публикации, а CLI явно сообщает пользователю, какой лимит
превышен и что нужно уменьшить scope либо увеличить настройку. То же сообщение
публикуется одним обновляемым служебным комментарием в исходном PR, даже если
translation PR ещё не создан.

Только переводчик получает небольшой релевантный фрагмент парного
глоссария YDB. Выбор делается по терминам текущего source-документа; в prompt
передаются source- и target-формулировки одной и той же glossary-секции. Это
контекст, а не текст для вставки, и не hardcoded замена слов. Контекст ограничен
8 наиболее релевантными секциями и 8 000 символами, чтобы большой changelog не
переполнял окно модели; при равной релевантности секции выбираются по anchor.

Если соответствующая ссылка в существующем target ведёт на другую существующую
target-статью, система отмечает её как вероятный legacy-дубликат. Она ничего не
сливает, не удаляет и не выбирает canonical path автоматически. Перевод
публикуется с `YELLOW`; короткий QA comment называет новый и существующий paths,
просит пользователя решить конфликт вручную и затем повторить `doc_verify`.

Если source-документ не помещается в настроенный размер чанка, он делится на минимальное число
крупных чанков по безопасным границам верхнеуровневых блоков. Внутри одного
чанка модель по-прежнему видит связный Markdown, а не отдельные предложения или
поля. Граница не может начинать новый чанк внутри вложенного списка или другого
элемента с отступом. По умолчанию размер исходного чанка равен 6000 символам.
Этот размер не применяется к correction prompt: предыдущий ответ модели и
correction note отправляются целиком, без локального запрета на длину запроса.
Prompt сообщает точные количества начальных и конечных `\\n`. Перед валидацией
сборщик восстанавливает эти source-owned boundary newlines, если модель их
обрезала; содержимое и placeholders при этом не изменяются.
Ответы собираются строго в исходном
порядке только из model responses и source-owned protected fragments.

При финальной проверке исходные parser diagnostics для source-owned YFM
сравниваются по bytes и допускаются без изменений. Любой новый или изменённый
diagnostic считается структурной ошибкой перевода.

При склейке collector восстанавливает из source отступ первой строки каждого
последующего чанка. Затем во всём собранном документе он детерминированно
добавляет потерянные моделью обязательные пустые строки рядом с заголовком либо
перед новым верхнеуровневым списком. Это применяется и внутри чанка, но только
к новым относительно source дефектам, поэтому исходное форматирование не
переписывается. Модель не может случайно превратить продолжение вложенного списка
в верхнеуровневый список или приклеить список к заголовку. Между обычными
абзацами и во всём остальном model Markdown сохраняется; обязательны успешный
parse без diagnostics и protected invariants. Совпадение
числа и типов блоков и byte-exact совпадение обычных non-field syntax slices не
требуется.

Translator получает упорядоченную карту только переводимых prose segments и
возвращает такую же полную карту по JSON Schema. Protected fragments отсутствуют
в model response. Runtime сам вставляет их между сегментами в исходном порядке,
поэтому code/URL/path/template невозможно потерять, продублировать или изменить.
Затем source или детерминированно локализованные fragments вставляются,
candidate повторно разбирается как Markdown/YFM без
diagnostics. Косметические и иные изменения model Markdown не отклоняются по
сравнению parser shape с source; существенную порчу структуры, смысла или полноты
проверяет critic. Невалидная segment map получает не более одной технической
попытки исправления. Повторный prompt сохраняет те же prose segments, включает
предыдущий ответ в блоке `<PREVIOUS_RESPONSE>` и причину валидации. Невалидный
chunk после correction делится по безопасной границе на два child chunk.
Невалидный child делится тем же способом, пока диапазон source blocks строго
уменьшается и остаётся безопасная граница, независимо от длины исходного chunk; успешные соседние chunks повторно не
переводятся. Невалидная segment map и build-breaking Markdown никогда не
публикуются. Финальный deterministic gate накладывает candidate на trusted
checkout base-ветки, запускает полный официальный Diplodoc build и отклоняет
любые `ERR` и ненулевой exit до commit/push; нефатальный `WARN` сам по себе не
блокирует публикацию. Это проверяет реальные YFM, TOC, include,
anchors и Markdown rules вместо дальнейшего расширения собственного parser.
После build checkout восстанавливается. При `doc_verify`
те же protected fragments сверяются с текущим target, включая вычисленные YDB
locale и Wikipedia destinations; остальные URL, path и code относительно
authoritative source изменять нельзя.

## Fenced comments

В fenced code используется минимальный quote-aware lexical scanner:

- C++, Java, .NET и JavaScript: `//`, `/* ... */`;
- Python, Bash и YAML: `#`;
- HTML: `<!-- ... -->`.

Scanner различает маркеры и те же символы внутри строковых литералов, но не
реализует полную грамматику языка. Для `text` fence поддерживается один простой
случай: непрозрачный синтаксис до первой пустой строки остаётся protected, а
поясняющий хвост после неё переводится целиком. Без такого разделителя `text`
fence и любой неизвестный язык целиком protected.

Кодовые и конфигурационные блоки восстанавливаются из source-owned fragments.
После восстановления комментарии с кириллицей переводятся отдельными вызовами и
заменяются внутри блока. Markdown-таблицы передаются с обычными разделителями
`|`; защищаются только код, ссылки и include/directive-фрагменты. После ответа
проверяются число строк и число столбцов каждой таблицы.

## Проверка качества всего PR

После локальных структурных проверок и безопасной нормализации runtime строит
один закрытый PRReviewContext: complete source/candidate inventory и operations,
полные review-owned source/candidate Markdown/YFM/YAML файлы, включая index/TOC,
allowed paths/operations, snapshot/candidate digests, technical-fragment validation
data и оба полных pinned RU/EN glossary. Source glossary берётся из authoritative
snapshot, target glossary из pinned translation base. Missing files/glossary
останавливают review. Если glossary сам входит в candidate, его изменённый текст
проверяется отдельно от pinned terminology reference.

Старый target prose не передаётся как образец формулировок или источник repair.
Точный candidate, включая сохранённый review checkpoint, является проверяемым
объектом. Operator context отделяется от authoritative content. Repository text
и glossary передаются как данные, вложенные инструкции не исполняются.

Один editor возвращает `{"files": {"<allowed path>": "<complete file>"}}`:
ровно все editable target text paths, включая unchanged. Строгая schema и parser
отвергают missing/unknown/duplicate keys, malformed/partial/empty files, binary,
deletion, traversal, out-of-scope и extra entries. Findings, patch и отдельный
repair call не заменяют полные готовые файлы.

Runtime атомарно применяет карту в памяти, проверяет source-owned technical
values, Markdown/YFM, links/anchors, metadata/plan constraints и assets,
затем запускает один полный Diplodoc build. Полный draft build до editor
запрещён. Technical literals видны с реальной окружающей разметкой:
разрешено безопасно улучшить inline-code оформление без изменения значения,
нельзя подменить URL, commands, identifiers или templates. TOC name corrections
допускаются только в явно разрешённом plan, с planner-owned пересчётом artifact
digest и provenance; structure/href/остальные rows, включая target-only navigation,
сохраняются. Arbitrary metadata overwrite запрещён.

После build один независимый read-only arbiter получает полный исправленный PR
и оба полных glossary. Он проверяет ровно built bytes. GREEN требует полного
валидного финального ответа, пустых findings, полного непустого coverage и
совпадающего context/candidate digest. No-op editor не отменяет build/arbiter;
editor byte changes, пустой ответ, отсутствующий arbiter или пустой checked set
не дают GREEN. Findings связываются с валидными paths/current snippets и
конкретной правкой. Semantic RED не запускает автоматический repair loop.
Публикация одна, после editor → validation/full build → arbiter. Изменение bytes
после arbiter требует нового полного review.

Оба prompt активно проверяют полноту/смысл, межфайловую терминологию и
идентичность сущностей даже без exact glossary mapping, commands/parameters/
identifiers, границы technical literals и читаемый Markdown, links/anchors,
H1/index/TOC и полный набор PR operations. Литературная полировка не требуется;
отсутствие словарной пары не оправдывает распад одной сущности на разные имена.

Editor/arbiter никогда не используют translator chunks или excerpts и не
получают filtered glossary. Admission проверяет verified input/context/output
capacity для точного serialized request с system/schema/JSON overhead и резервом
полных output files. Unknown capability и oversize дают typed terminal failure
до publication; lossy truncation, summary, chunk fallback и partial GREEN
запрещены. Пара glossary witness содержит 225432 UTF-8 bytes. Исторический
48000-character budget и старые probes недостаточны для нового контракта.
Retry policy едина с REQUIREMENTS_RU.md §5: максимум две primary attempts на
роль; optional single editor fallback после content-filter использует полный
контекст и новый admission. Translator retries/splitting остаются отдельными.

Continuation повторно переводит только pending documents, но любой semantic
review снова включает весь PR/glossary, в том числе ранее accepted files.
Новая contract version и source/candidate/glossary manifests/digests связывают
checkpoint; старые chunk-review checkpoints отвергаются fail closed.
Offline fakes доказывают orchestration. Semantic acceptance требует live
full-context provider probe и независимой проверки полных результатов
реального witness #50839/#54590, описанного в testing.md.

## Исторический translator regression: сбой 36379127309

Технический цикл: TRANSLATE → локальная валидация → одна TRANSLATE correction
с прежним ответом и конкретной причиной → безопасное деление, если correction
невалидна. Любой многоблочный chunk может делиться; старый порог 4000 символов
блокировал восстановление штатных chunks при бюджете prompt 6000. Неделимый
невалидный chunk не публикуется. Каждый отказ пишет безопасный код валидации
без model response и source prose; успешные соседние chunks не повторяются.

Текущий утверждённый порядок quality stage: полный PR editor, затем
строгий Diplodoc build и только после него полный PR arbiter перед публикацией. По умолчанию переводчик — DeepSeek V4 Flash, critic-editor —
YandexGPT 5.1 (`YDBDOC_MODEL_CRITIC` позволяет явно выбрать модель).
