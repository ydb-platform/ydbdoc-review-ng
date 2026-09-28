# Модели и API Yandex Cloud

## Обязательные настройки translate

- `temperature = 0`.
- Reasoning полностью отключён.
- Translate возвращает raw Markdown и проходит локальную проверку protected fragments.
- Direction и semantic critic возвращают structured output по JSON Schema;
  raw JSON повторно проверяется локально независимо от гарантий провайдера.
- Каждая начатая attempt сохраняется в durable state с параметрами, входом,
  status/error, сырым ответом и usage при их наличии.
- Любой полученный response, включая malformed или семантически неудачный,
  сохраняет фактическую cost. Если response/usage отсутствуют и provider не
  сообщил billable cost, сохраняется `NULL`/unknown, а не фиктивный `0`.
  Достоверно сообщённая нулевая стоимость сохраняется как `0`.
- Явный content-filter допускает ровно один повтор идентичного request в пределах
  `max_attempts = 2`; обе attempts аудируются и оплачиваются, а truncation и
  прочие non-final статусы не повторяются.
- Raw-Markdown `TRANSLATE`/`REPAIR` chunk, дважды завершённый content-filter,
  допускает один deterministic split по ближайшей к середине top-level block
  boundary. Дочерний chunk при повторном content-filter делится тем же способом,
  пока диапазон top-level blocks строго уменьшается; успешные соседние chunks не
  перезапускаются. Невалидная correction и content-filter на correction также допускают split
  TRANSLATE; другие provider errors деление не включают.
- В raw-Markdown prompts каждый placeholder требуется ровно один раз в своём
  source top-level block. Только независимые `inline_code` и атомарные `template`
  могут менять порядок внутри переводимого поля; остальные placeholders и
  link/image пары сохраняют порядок и вложенность.

## Проверенные модели

### YandexGPT 5.1

Нативный Foundation Models API принимает `reasoningOptions.mode = DISABLED` и
JSON Schema. В интеграционном прогоне PR 51079 все 361 принятых ответа сообщили
`reasoningTokens = 0`. Канонический текущий model URI:
`gpt://<folder>/yandexgpt-5.1`; суффикс `/latest` не используется в default,
но явный `YDBDOC_MODEL` сохраняет возможность выбрать другой URI. В runtime это
fallback-модель для перевода и отдельная модель critic-editor/final critic
по умолчанию (`YDBDOC_MODEL_CRITIC`).

### DeepSeek V4 Flash

OpenAI-compatible endpoint принимает `reasoning_effort = none`, temperature 0 и
JSON Schema. В runtime модель используется как основная модель перевода; при
ошибке провайдера или content filter запрос повторяется через YandexGPT.

### gpt-oss-120b

Проверенный endpoint отвергает `reasoning_effort = none` и допускает только
`low`, `medium` или `high`. Эта модель не подходит для роли `translate`, пока
полное отключение reasoning является требованием.

## Секреты

Ключи и идентификаторы читаются только из environment/GitHub secrets. Их нельзя
писать в prompts, логи, fixtures, knowledge bank или git history.
