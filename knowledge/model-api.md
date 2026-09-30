# Модели и API Yandex Cloud

Полный PR review contract утверждён 2026-09-30, но ещё не реализован и не
provider-validated на baseline `2a1c268`. Возможности моделей ниже подтверждают
лишь указанные исторические API-факты, не новую full-context schema/capacity.

## Translator и review boundaries

- `temperature = 0`, reasoning полностью отключён.
- Whole-file/source-only translator возвращает structured prose segment map;
  runtime вставляет protected code/URL/path/template. Old target prose не является
  model input для перевода или repair. Translator сохраняет structural chunking
  и релевантный glossary selection по текущему source chunk.
- Direction, translator, PR editor и arbiter используют JSON Schema и повторную
  строгую локальную проверку raw response, включая duplicate JSON keys.
- Один PR editor получает полный закрытый context: inventory и operations,
  все review-owned source/candidate text files, allowed paths, snapshot/candidate
  digests, technical validation data и оба полных pinned RU/EN glossary.
  Source glossary читается из authoritative snapshot, target glossary из pinned
  translation base. Missing/unreadable glossary или manifest file терминальны.
- Editor schema: `{"files": {"<allowed target path>": "<complete corrected file>"}}`
  с `additionalProperties=false`, ровно все editable text paths, включая unchanged.
  Missing/unknown/duplicate/malformed/partial/empty entries, binary, deletion,
  traversal, out-of-scope paths и extra fields отвергаются fail closed.
- Runtime проверяет source-owned technical values и metadata/plan constraints
  и атомарно применяет карту в памяти. Независимый read-only arbiter видит полный
  окончательный результат с полным glossary.
- Arbiter возвращает строгие verdict/findings с полным coverage и runtime-verified
  context/candidate digest. GREEN требует валидный полный финальный ответ, пустые
  findings, полное непустое coverage и совпадение digest. Editor byte changes,
  пустой response, missing arbiter или пустой checked set не означают GREEN.
- Review видит реальные technical literals с Markdown-границами. Source-owned
  значения не меняются; active checks включают межфайловую entity identity даже
  без exact glossary mapping, completeness, commands/parameters, readable
  technical formatting, links/anchors, H1/index/TOC и PR operations.
- Полный editor → runtime validation/apply → полный arbiter → semantic verdict.
  No-op editor не отменяет arbiter. GREEN означает корректный перевод; RED
  findings идут непосредственно в отчёт, автоматически не исправляются и никуда
  не передаются. Build/CI не участвуют в семантическом вердикте.

## Capacity и одна bounded retry policy

Review requests неделимы. Admission использует verified per-provider
input/context/output capabilities и проверенный tokenizer либо доказуемо
консервативную bound для точного serialized request: instructions, schema,
delimiters, glossary, operator context, все тексты и резерв полных output files
с JSON escaping. Повторный admission выполняется после edits для arbiter.
Unknown capability даёт typed configuration failure, oversize даёт terminal
`review_context_limit_exceeded` или `review_output_limit_exceeded` до publication.
Нельзя сокращать glossary, отправлять excerpts/translator chunks, summaries
или считать частичную проверку GREEN.

Для PR editor/arbiter максимум две primary attempts на роль суммарно:
schema-invalid success получает один retry с parser reason; content-filter или
non_final/truncated получает один идентичный retry. Смешанные ошибки не
обнуляют счётчик. Иные ошибки терминальны. Только exhausted editor content-filter
может дать одну fallback attempt, если путь оставлен в production: тот же полный
context/schema, новый capacity admission. Следующий отказ терминален; arbiter
fallback не вводится. Это единая policy REQUIREMENTS_RU.md §5.

Translator content-filter retry/split остаётся translator-only: максимум две
attempts на chunk, затем безопасное рекурсивное деление по top-level boundaries
при строго уменьшающемся диапазоне. Невалидная correction также допускает split;
non-final translator responses и прочие provider errors не запускают review
fallback. Успешные соседние chunks не повторяются.

## Audit и provider acceptance

Каждая начатая attempt сохраняет request, response/status/error, timestamps,
model и usage/cost. Translate имеет article `target_path`; PR editor/arbiter
имеют `target_path = NULL`, явный PR scope, contract version и context digest.
PR costs не приписываются произвольной статье и не смешиваются с историческими
unattributed rows. Malformed/неудачный ответ сохраняет фактическую cost;
неполученный usage без billable cost даёт NULL/unknown, достоверный ноль остаётся 0.

Pinned witness glossary уже содержит 225432 UTF-8 bytes. Исторический лимит
48000 characters и старые whole-excerpt probes не подтверждают вместимость или
качество нового пути. Production builders/parsers должны пройти real-provider
full-context probe: translator map, multi-file editor с исправлением двух файлов
и межфайлового дефекта, runtime validation/apply, затем полный read-only arbiter.
Каждый реально используемый primary/fallback provider проверяется своим полным
контрактом; fallback probe нужен только при сохранённом production пути.
Offline fakes доказывают orchestration. До независимого content review полного
#50839/#54590 witness semantic acceptance не заявляется.

## Исторически проверенные API-возможности моделей

### YandexGPT 5.1

Нативный Foundation Models API принимает `reasoningOptions.mode = DISABLED` и
JSON Schema. В интеграционном прогоне PR 51079 все 361 принятых ответа сообщили
`reasoningTokens = 0`. Канонический текущий model URI:
`gpt://<folder>/yandexgpt-5.1`; суффикс `/latest` не используется в default,
но явный `YDBDOC_MODEL` сохраняет возможность выбрать другой URI. В runtime это
fallback-модель для перевода и отдельная модель critic-editor
по умолчанию (`YDBDOC_MODEL_CRITIC`).
DeepSeek также является независимым arbiter по умолчанию после YandexGPT
critic-editor; `YDBDOC_MODEL_ARBITER` позволяет выбрать третью модель явно.

### DeepSeek V4 Flash

OpenAI-compatible endpoint принимает `reasoning_effort = none`, temperature 0 и
JSON Schema. В runtime модель используется как основная модель перевода; при
ошибке провайдера или content filter запрос повторяется через YandexGPT.
Runtime извлекает обычные и кешированные входящие токены из OpenAI usage и
считает стоимость DeepSeek V4 Flash по опубликованному тарифу на 2026-09-29:
0,3 руб./1000 входящих, 0,075 руб./1000 кешированных входящих и
0,5 руб./1000 исходящих токенов. Поэтому успешный ответ модели имеет числовую
стоимость; unknown остаётся только при отсутствии usage и provider billable cost.
Источник тарифа: <https://aistudio.yandex.ru/ru/docs/ai-studio/pricing>.

### gpt-oss-120b

Проверенный endpoint отвергает `reasoning_effort = none` и допускает только
`low`, `medium` или `high`. Эта модель не подходит для роли `translate`, пока
полное отключение reasoning является требованием.

## Секреты

Ключи и идентификаторы читаются только из environment/GitHub secrets. Их нельзя
писать в prompts, логи, fixtures, knowledge bank или git history.
