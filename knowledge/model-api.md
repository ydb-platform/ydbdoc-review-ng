# Модели и response contracts

Production: DeepSeek V4 Flash для direction, translator, TOC strings, critic,
arbiter. YandexGPT только в `doc_model_probe`.

Контекст 1 048 576. `max_tokens` = остаток после размера полного wire request
в UTF-8 bytes (для этого расчёта 1 byte = 1 token). Не путать с рублёвым
дневным бюджетом. `NON_FINAL` → чанк непроверен, итог RED.

## Контракты

- Direction: только `translation_required`, `direction`, `reason`. Per-file
  mirror actions считает Python (`mirror_classified_files`), не модель.
- Translator / TOC strings: полная JSON ID-map запрошенных сегментов.
- Critic (canon §4.1): OpenAI tool loop на workspace — `read`, `grep`,
  `apply_patch`, `finish`; mandatory re-read after patches; published bytes =
  runtime-applied workspace. Whole-file `{"files":…}` **не** primary path.
  Пустой scope → no-op `finish`. Plan: `tool-using-critic-plan.md`.
- Arbiter: только `verdict` + `findings`. GREEN → пустые findings; YELLOW/RED →
  ≥1. Missing/unreviewed → `null` line/snippet. No repair loop.

`reasoning_effort` (tip): critic `medium`, arbiter `none`, else `none`.

Один retry на provider/malformed/protocol для direction, translator, critic,
arbiter. Critic без успешного `finish` не публикует reviewed bytes.

Attempts аудируются с cost; unknown = `NULL`. Секреты только из env/GitHub
secrets.

## Clients today vs tools

`YandexOpenAIClient` / `NativeYandexClient` ещё **не** шлют `tools` и не
парсят `tool_calls` (`clients.py`). P0 live probe **PASS**: DeepSeek returns
`finish_reason=tool_calls` with `content=null` and accepts `role=tool`
round-trip (`knowledge/p0-deepseek-tools-probe.md`). P1c must not map that
shape to `NON_FINAL` / `EMPTY_TEXT`.
