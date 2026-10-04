# Текущее состояние

## Согласованный контракт (thin pipeline, 2026-10-04)

Semantic flow в `REQUIREMENTS_RU.md`:

1. Файлы PR + дотянутые missing-target зависимости.
2. Direction: только «нужен перевод?» + направление (Python владеет Git-ops).
3. Thin whole-file DeepSeek (или unique string replacements без модели).
4. Publication gates fail-closed: source-locale echo, split-backtick, missing
   includes. Один retry; иначе null.
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

## Tip pin

Workflow `ydbdoc-review.yml` uses
`ydb-platform/ydbdoc-review-ng/.github/actions/doc-review@v1.0.1`.
Move `v1.0.1` with the current tip after push.
