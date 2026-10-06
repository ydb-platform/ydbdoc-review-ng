# Текущее состояние

## Согласованный контракт (thin pipeline, 2026-10-04)

Semantic flow в `REQUIREMENTS_RU.md`:

1. Файлы PR + дотянутые missing-target зависимости.
2. Direction: только «нужен перевод?» + направление (Python владеет Git-ops).
3. Thin whole-file DeepSeek (или unique string replacements без модели).
4. Publication gates fail-closed: source-locale echo, split-backtick, missing
   includes, ATX heading blank lines, unlabeled fence openers. Один retry;
   иначе null.
   Insert-only splice: blank line before a heading-starting hunk.

## Fence unwrap (2026-10-04)

Thin path has no placeholders. `unwrap_thin_response` used to drop the first
and last fence of any file that starts with ` ``` `. That ate ` ```bash ` on
`check-backup.md` (#55048 RED). Unwrap only ` ``` ` / ` ```markdown ` / ` ```md `
wrappers around the whole response.

## Nested TOC + soft wrap (2026-10-05)

Metadata no longer flat-appends dependency pages as basename tails. It applies
source TOC structure via `remove_toc_hrefs` + `apply_toc_delta`, then translates
Cyrillic labels. Thin/hunk output runs `join_soft_wrapped_prose` so YFM soft
breaks are repaired without rewriting untouched unique-replacement files.
5. Tool-using critic **снят**. Reviewed = gated publish.
6. Arbiter на reviewed bytes: GREEN / YELLOW / RED (judge-only, no repair).
7. YELLOW = успех; RED = continue / ручная правка + `doc_verify`.
8. Режимы: `doc_translate`, `doc_verify`, `doc_continue`.

## Live evidence that forced the cutover

- [#54993](https://github.com/ydb-platform/ydb/pull/54993) / [#55003](https://github.com/ydb-platform/ydb/pull/55003)
  (source [#46837](https://github.com/ydb-platform/ydb/pull/46837)): critic
  burn / mangled identifiers.
- [#54994](https://github.com/ydb-platform/ydb/pull/54994) (source
  [#42314](https://github.com/ydb-platform/ydb/pull/42314)): CI `recovery.md`
  published with **41 lines still Russian**; critic budget RED.
- Local PoC: same scope via thin DeepSeek → 8/8 OK, 0 Cyrillic, 0 mangling
  (`SINTJURI_SECRET_KEY` + `YANDEX_CLOUD_FOLDER`).

## TOC insert order (2026-10-04)

`apply_toc_delta` repositions a source-new identity that is already in the
target (metadata `_append_toc` parks it at the YAML `items` tail). Example:
[#55007](https://github.com/ydb-platform/ydb/pull/55007) `toc_i.yaml` Logging
must sit after Ya Make, not after Changelog.

`doc_verify` on an already-GREEN PR does not rewrite TOC; restart
`doc_translate` on the source PR after moving `v1.0.1`.

## Glossary and include gates (2026-10-04)

[#55024](https://github.com/ydb-platform/ydb/pull/55024) RED: thin whole-file of
existing EN glossary (missing-anchor exception) died on provider `transport`;
`structure.md` nulled because include-gate saw only in-flight files, not
`_includes/tpch-dataset-note.md` already on main.

Now: existing glossary file stops *dependency* scope; missing anchors are
diagnostics. Insert-only inventory delta on an existing EN glossary is a
surgical hunk (thin-translate the new section, append), not whole-file.

Include gate accepts target-snapshot files that this PR did not touch.

## Surgical locate (2026-10-05)

[#55170](https://github.com/ydb-platform/ydb/pull/55170) `authentication.md`
went `whole_file` because `_locate_target_span` required a unique `](url)`.
#53033 replace+insert had only `` `authorization code` ``. Locate now also
uses unique inline code and `{#anchor}`. Keep #55170 as accepted scope drift;
fix is for the next translate.

## Bilingual skip + presentation_map (2026-10-06)

[#55240](https://github.com/ydb-platform/ydb/pull/55240) / [#55243](https://github.com/ydb-platform/ydb/pull/55243)
re-translated bilingual source PRs and then `presentation_map` globally replaced
`CPU`/`RATE`/`GAUGE` inside already-correct EN. Python now skips Markdown pairs
present in both locales of the inventory. Surgical hunks no longer apply
presentation_map. Fallback wrap requires real `\\_` escapes and identifier
boundaries, not a bare-token `str.replace`.

## Nested translation skip

`doc_translate` on a PR whose body has `<!-- ydbdoc-source-pr: -->` is a no-op
(«Этот PR уже является переводом.»). Stops #55244-style EN→RU of a translation PR.

## Tip pin

Workflow `ydbdoc-review.yml` uses
`ydb-platform/ydbdoc-review-ng/.github/actions/doc-review@v1.0.1`.
Move `v1.0.1` with the current tip after push.
