# Модели и API Yandex Cloud

## Обязательные настройки translate

- `temperature = 0`.
- Reasoning полностью отключён.
- Строгий structured output по JSON Schema.
- Raw JSON повторно проверяется локально независимо от гарантий провайдера.
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
  перезапускаются. Correction и другие provider errors этот split не включают.
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
но явный `YDBDOC_MODEL` сохраняет возможность выбрать другой URI. Это основная
подтверждённая модель перевода.

### DeepSeek V4 Flash

OpenAI-compatible endpoint принимает `reasoning_effort = none`, temperature 0 и
JSON Schema. Модель пригодна как ограниченный fallback, но на одном сложном поле
завершила ответ по лимиту, поэтому не считается гарантией успешного перевода.

### gpt-oss-120b

Проверенный endpoint отвергает `reasoning_effort = none` и допускает только
`low`, `medium` или `high`. Эта модель не подходит для роли `translate`, пока
полное отключение reasoning является требованием.

## Секреты

Ключи и идентификаторы читаются только из environment/GitHub secrets. Их нельзя
писать в prompts, логи, fixtures, knowledge bank или git history.
