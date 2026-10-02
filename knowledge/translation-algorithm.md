# Алгоритм перевода и проверки

## Scope

1. Immutable inventory source PR.
2. Один direction call: `translation_required`, `direction`, `reason`.
3. Python зеркалит Git: translate / delete / rename / resource / TOC.
4. Рекурсия по внутренним ссылкам: нет target у A1 → A1 в группу, даже если
   A1 не менялся в PR. Есть target → стоп, в review не входит.
5. Glossary без target-anchor → целиком в scope.
6. Лимиты: `YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE` (группа статьи),
   `YDBDOC_MAX_SOURCE_CHARACTERS` (один файл).

## Prep + Translator

Placeholders + identifier atoms (не рвать `BS\_CONTROLLER` на ESCAPE).
Optional presentation map с old target. Целый файл одним request. Old target
в prompt только как presentation reference. Segment ID map → runtime restore
+ apply map. Собранный UTF-8 → **draft** soft-publish (diagnostics ≠ product).

## TOC

Python: source TOC before/after → дельта → apply к target TOC. DeepSeek:
только новые/изменённые видимые строки. Нет target TOC → только entries этого
PR. Покрыть тестами.

## Critic / arbiter

Critic: обязательный gate на source + draft + optional presentation-reference
→ полные `{"files": {...}}`, **reviewed** push. Fail/503/unreviewed → RED,
arbiter на сырой dump не вызывается. Arbiter: только reviewed bytes,
GREEN/YELLOW/RED + findings, без автопочинки. YELLOW не открывает checkpoint.
