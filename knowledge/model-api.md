# Модели и response contracts

Production: DeepSeek V4 Flash для direction, thin translator, TOC strings,
arbiter. YandexGPT только в `doc_model_probe`.

Контекст 1 048 576. `max_tokens` = остаток после размера полного wire request
в UTF-8 bytes (для этого расчёта 1 byte = 1 token). Не путать с рублёвым
дневным бюджетом.

## Контракты

- Direction: только `translation_required`, `direction`, `reason`. Per-file
  mirror actions считает Python (`mirror_classified_files`), не модель.
- Translator (thin): полный target Markdown; developer prompt фиксирует YFM /
  no split-backtick / no whole-file fence. JSON segment map → reject.
- TOC strings: JSON ID-map запрошенных сегментов.
- Critic tools: **сняты** с production. Quality gate = Python publication gates.
- Arbiter: только `verdict` + `findings`. GREEN → пустые findings; YELLOW/RED →
  ≥1. Missing/unreviewed → `null` line/snippet. No repair loop.

`reasoning_effort` (tip): arbiter `none`, else `none`.

Один retry на provider/malformed для direction, translator, arbiter.
Publication gates: один retry translator, затем null.

Attempts аудируются с cost; unknown = `NULL`. Секреты только из env/GitHub
secrets.
