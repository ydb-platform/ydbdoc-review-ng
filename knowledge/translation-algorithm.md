# Алгоритм перевода и проверки

## Выбор файлов

Runtime строит полный immutable inventory source PR. Простая DeepSeek-проверка
определяет, нужен ли перевод, а Python фиксирует операцию для каждого
пути: translate, delete, rename, TOC или resource copy.

Для каждой переводимой статьи runtime рекурсивно обходит внутренние Markdown-
ссылки. Если source B есть, а target B нет, B добавляется в frozen group.
Если target B уже есть, рекурсия на нём останавливается, а B не входит в
текущую группу review.

## Whole-file translator

Каждый Markdown/YFM source-файл отправляется модели целиком одним
request. Внутридокументного translator chunking нет. Runtime защищает URL,
paths, code, templates и другие source-owned fragments, а DeepSeek возвращает
строгую JSON-карту всех prose segment IDs. Existing target в translator prompt не
попадает.

Для файла выбираются все релевантные парные секции glossary. Ограничений по
числу секций или символов нет. Runtime вставляет protected
fragments и собирает полный UTF-8 candidate.

Malformed JSON, неполная ID-map или provider error допускают одну техническую
повторную попытку. Если целый файл извлечь нельзя, target остаётся
`null` для critic. Если файл собран, он публикуется даже с ошибками
Markdown/YFM, links, anchors, source echo и protected fragments. Эти ошибки
становятся diagnostics для critic, arbiter и техписа.

## TOC и assets

Python парсит source TOC до и после PR, вычисляет структурную дельту и
применяет её к полному текущему target TOC. DeepSeek переводит только новые
или изменённые видимые строки. Если target TOC не было, в новый файл
попадают только добавленные и изменённые в этом PR entries, а не весь source
TOC. Совпадающий `href` обновляется, дубликат не создаётся.

Images, PDF, binary и diagram sources не переводятся, но переносятся, удаляются
и переименовываются без потери bytes. Модели получают только manifest.

## Critic и arbiter

Critic получает полные source/target пары, TOC before/after context, relevant
paired glossary sections и manifest непреобразуемых файлов. Он не возвращает
findings, а сразу возвращает точную карту полных исправленных target-файлов.
Каждый успешный чанк сразу применяется и публикуется. Ошибка одного
чанка не откатывает остальные.

Arbiter заново формирует чанки по финальным файлам и возвращает
GREEN, YELLOW или RED. Общий verdict равен худшему verdict чанков. YELLOW
операционно ведёт себя как RED. Findings идут прямо в отчёт и не ремонтируются.

Полная source/target-пара не делится. Если она одна не помещается в контекст,
вызов не выполняется, остальные чанки проверяются, а итог RED называет
непроверенный path. При нуле текстовых пар critic и arbiter всё равно вызываются
с полным inventory и manifest. Critic возвращает `{"files": {}}`, а итог берётся
из verdict арбитра.
