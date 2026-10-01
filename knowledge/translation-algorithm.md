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

## Translator

Целый файл одним request. Existing target не в prompt. Segment ID map →
runtime вставляет protected fragments. Одна техническая коррекция. Собрали
UTF-8 → публикуем всегда.

## TOC

Python: source TOC before/after → дельта → apply к target TOC. DeepSeek:
только новые/изменённые видимые строки. Нет target TOC → только entries этого
PR. Покрыть тестами.

## Critic / arbiter

Critic: полные пары + glossary + manifest → `{"files": {...}}`, сразу push.
Чанки по целым парам при необходимости. Arbiter: GREEN/YELLOW/RED + findings,
без автопочинки. Худший чанк побеждает. YELLOW не открывает checkpoint.
