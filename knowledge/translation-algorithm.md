# Алгоритм перевода и проверки

## Scope

1. Immutable inventory source PR.
2. Один direction call: `translation_required`, `direction`, `reason`.
3. Python зеркалит Git: translate / delete / rename / resource / TOC.
4. Рекурсия по внутренним ссылкам: нет target у A1 → A1 в группу, даже если
   A1 не менялся в PR. Есть target-файл → стоп, в review не входит.
5. Glossary: нет EN-файла → как missing-target. EN-файл есть, точного anchor
   нет → diagnostic, целиком не переводим.
6. Лимиты: `YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE` (группа статьи),
   `YDBDOC_MAX_SOURCE_CHARACTERS` (один файл).

## Thin translator

Unique string replacements без модели, если возможно. Insert-only дельта на
существующий target: модель переводит только вставленный фрагмент, Python
дописывает его в конец EN. Иначе **thin whole-file**: полный source Markdown →
DeepSeek → полный target Markdown. Placeholders нет.
После ответа: снять только обёртку ` ```markdown ` вокруг всего файла, не
открывающий fence документа; normalize split-backtick, затем publication gates
(`source_locale_echo`, `split_backtick_identifiers`, `missing_include_target`,
`heading_blank_lines`, `unlabeled_fence_opener`).
Insert-only: Python добавляет пустую строку перед ATX-заголовком на стыке с
существующим EN, если модель её съела.
Один retry; провал → файл null, не soft-publish полу-EN.

## TOC

Python: source TOC before/after → дельта → apply к target TOC. DeepSeek:
только новые/изменённые видимые строки. Нет target TOC → только entries этого
PR. Если новая source-запись уже сидит в target (обычно хвост после
metadata-append), delta переставляет её к предыдущему after-соседу, а не
оставляет in-place. Покрыть тестами.

## Quality / arbiter

Tool-using critic снят. Reviewed = gated publish. Arbiter: scope = PR delta +
previous EN; Python drop out-of-delta findings. YELLOW не открывает checkpoint.
RED на дырах / unreviewed / arbiter findings.
