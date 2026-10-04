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

## Thin translator

Unique string replacements без модели, если возможно. Иначе **thin whole-file**:
полный source Markdown → DeepSeek → полный target Markdown. Placeholders нет.
После ответа: normalize split-backtick, затем publication gates
(`source_locale_echo`, `split_backtick_identifiers`, `missing_include_target`).
Один retry; провал → файл null, не soft-publish полу-EN.

## TOC

Python: source TOC before/after → дельта → apply к target TOC. DeepSeek:
только новые/изменённые видимые строки. Нет target TOC → только entries этого
PR. Покрыть тестами.

## Quality / arbiter

Tool-using critic снят. Reviewed = gated publish. Arbiter: scope = PR delta +
previous EN; Python drop out-of-delta findings. YELLOW не открывает checkpoint.
RED на дырах / unreviewed / arbiter findings.
