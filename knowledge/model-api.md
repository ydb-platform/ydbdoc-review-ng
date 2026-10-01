# Модели и response contracts

Production: DeepSeek V4 Flash для direction, translator, TOC strings, critic,
arbiter. YandexGPT только в `doc_model_probe`.

Контекст 1 048 576. `max_tokens` = остаток после размера полного wire request
в UTF-8 bytes (для этого расчёта 1 byte = 1 token). Не путать с рублёвым
дневным бюджетом. `NON_FINAL` → чанк непроверен, итог RED.

## Контракты

- Direction: `translation_required`, `direction`, `reason`. Git inventory —
  только Python.
- Translator / TOC strings: полная JSON ID-map запрошенных сегментов.
- Critic: `{"files": {"path": "complete UTF-8"}}`; пустой scope → `{"files": {}}`.
- Arbiter: только `verdict` + `findings`. GREEN → пустые findings; YELLOW/RED →
  ≥1. Missing/unreviewed → `null` line/snippet.

Один retry на provider/malformed для direction, translator, critic, arbiter.
Critic partial response не применяется.

Attempts аудируются с cost; unknown = `NULL`. Секреты только из env/GitHub
secrets.
