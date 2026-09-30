# Модели и API Yandex Cloud

## Обязательные настройки translate

- `temperature = 0`.
- Reasoning полностью отключён.
- Translate возвращает structured map только переводимых prose segments. Runtime
  сам вставляет protected code/URL/path/template между ними; модель не возвращает
  и не контролирует placeholders.
- Direction и translate возвращают structured output по JSON Schema;
  raw JSON повторно проверяется локально независимо от гарантий провайдера.
- Критик сравнивает полные актуальные source PR files, полные соответствующие
  translation PR files и полный glossary и возвращает полные исправленные файлы.
  Его точный JSON prompt приведён в REQUIREMENTS_RU.md. Runtime проверяет и
  применяет исправления; независимый арбитр возвращает GREEN/YELLOW/RED по степени
  проблем. Остаточные замечания идут непосредственно в отчёт, автоматически не
  исправляются и никуда не передаются. Build/CI не участвуют в semantic verdict.
  Prompt живой и модифицируется при отладке без изменения orchestration.
- Каждая начатая attempt сохраняется в durable state с параметрами, входом,
  status/error, сырым ответом и usage при их наличии.
- Любой полученный response, включая malformed или семантически неудачный,
  сохраняет фактическую cost. Если response/usage отсутствуют и provider не
  сообщил billable cost, сохраняется `NULL`/unknown, а не фиктивный `0`.
  Достоверно сообщённая нулевая стоимость сохраняется как `0`.
- Явный content-filter допускает ровно один повтор идентичного request в пределах
  `max_attempts = 2`; обе attempts аудируются и оплачиваются, а truncation и
  прочие non-final статусы не повторяются.
- `TRANSLATE` chunk, дважды завершённый content-filter,
  допускает один deterministic split по ближайшей к середине top-level block
  boundary. Дочерний chunk при повторном content-filter делится тем же способом,
  пока диапазон top-level blocks строго уменьшается; успешные соседние chunks не
  перезапускаются. Невалидная correction и content-filter на correction также допускают split
  TRANSLATE; другие provider errors деление не включают.
- Translator schema не содержит placeholders: полная карта segment IDs
  проверяется локально, затем runtime вставляет source-owned fragments в
  исходном порядке.
- Только translator получает для каждого отдельного чанка те пары project
  glossary, исходные термины которых встречаются в этом чанке. Критик и арбитр
  получают полные файлы и полный glossary.

## Проверенные модели

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
