# Алгоритм перевода и проверки

## Scope

1. Immutable inventory source PR.
2. Отдельная классификация каждой пары по изменениям и контексту блоков;
   см. [pair-classification.md](pair-classification.md).
3. Python зеркалит Git: translate / delete / rename / resource / TOC.
   Пара пропускается только при подтверждённой согласованности изменений,
   а не по наличию обоих путей в inventory. Смешанные направления допустимы.
4. Рекурсия по внутренним ссылкам: нет target у A1 → A1 в группу, даже если
   A1 не менялся в PR. Есть target-файл → стоп, в review не входит.
5. Glossary: нет EN-файла → как missing-target. EN-файл есть, точного anchor
   нет → diagnostic, целиком не переводим.
6. Лимиты: `YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE` (группа статьи),
   `YDBDOC_MAX_SOURCE_CHARACTERS` (один файл).

## Thin translator

Unique string replacements без модели, если возможно. Иначе выровненные hunks:
привязка к EN по уникальному якорю из `before` (link destination → inline code
→ `{#anchor}`); модель переводит только hunk; presentation-map на stitched EN
не накладывается. Insert-only без якоря: модель переводит вставленный фрагмент,
Python дописывает в конец EN. Delete-only, если удалённые hunks не находятся в
EN → **noop** (оставить EN, не whole-file). Иначе **thin whole-file**.
Placeholders нет.
После ответа: снять только обёртку ` ```markdown ` вокруг всего файла, не
открывающий fence документа; затем технические publication gates
(`utf8_invalid`, `missing_include_target`). Include внутри примеров кода не
считается зависимостью. Регистр, кириллица, пустые строки и fence без языка
не блокируют файл. Глобальных split-backtick/soft-wrap исправлений нет:
ответ сохраняется без изменений кода, YAML и переносов Markdown.
При сборке отдельных chunks/hunks сохраняется граница блоков.
Dependency/new page TOC: structural `apply_toc_delta` от source TOC (секции и
соседи), не flat basename-append; затем TOC string translate.
Один retry при техническом провале; повторный провал → файл null.
См. [аудит ограничений](publication-gate-audit-2026-10-29.md).

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
