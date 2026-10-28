# Модели и response contracts

Production: DeepSeek V4 Flash для direction, thin translator, TOC strings,
arbiter. YandexGPT только в `doc_model_probe`.

Документация модели указывает контекст 1 048 576. Локальная оценка входа
консервативна: 1 UTF-8 byte = 1 token. `max_tokens` ограничивается одновременно
остатком окна, максимумом генерации DeepSeek V4 Flash 262144 и меньшим лимитом
конкретного запроса. Классификатор пары запрашивает не более 4096 токенов ответа.
При HTTP 400 с явными фактическими input tokens и context limit клиента
допускается один повтор с уменьшением только ответа до оставшейся ёмкости.
Вход не обрезается. Если вход сам не помещается или ошибка другая, повтор
с тем же неподходящим запросом не выполняется. Обе попытки аудируются.
Не путать лимиты токенов с рублёвым дневным бюджетом.

## Контракты

- Direction: решение по каждой паре, см. [pair-classification.md](pair-classification.md).
  Git mirror actions считает Python, не модель.
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
