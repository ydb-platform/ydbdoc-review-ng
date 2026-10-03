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
Если есть old target и source before/after PR (merged: parent/merge, не
текущий `main`): surgical update (уникальные URL/строки без модели, иначе
модель только на hunks, Markdown с placeholders). Иначе целый файл одним
Markdown request. Hunk: old target = semantic baseline. Whole-file fallback:
presentation reference. Restore placeholders из source. Без глобального
presentation map на unique dest. Собранный UTF-8 → **draft** soft-publish
(diagnostics ≠ product).

## TOC

Python: source TOC before/after → дельта → apply к target TOC. DeepSeek:
только новые/изменённые видимые строки. Нет target TOC → только entries этого
PR. Покрыть тестами.

## Critic / arbiter

Critic: tool-using DeepSeek всегда, включая unique dest. Задание — source
delta + previous EN, не весь файл. Патч вне touched lines отклоняется.
Fail/503/protocol/unreviewed → RED; arbiter на сырой dump не вызывается.
Arbiter: тот же scope; Python drop out-of-delta findings → GREEN если дельта
верна. YELLOW не открывает checkpoint.
