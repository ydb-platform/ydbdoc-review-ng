# Конвейер разработки

1. Из канонических требований выделяется одна атомарная задача.
2. Разработчик сначала пишет failing unit test, затем минимальную реализацию.
3. Независимый tester проверяет публичное поведение, non-vacuity и regression.
4. Конкретный FAIL возвращается как bounded remediation.
5. PASS фиксируется отдельным task commit; перед релизом выполняются полный
   non-live suite, static checks, package build и installed smoke.

`v1.0.x` поставляет `doc_translate` и `doc_verify`; `1.1.0` реализует
`doc_continue` через CLI и composite action. История разработки,
временные отчёты и paid-model transcripts не входят в публичный репозиторий.

До публикации `v1.1.0` dispatcher повторяет release gate на точном финальном tree:
полный offline suite, Ruff/format/mypy/diff, clean wheel/sdist и installed smoke
трёх режимов, затем независимая release review. Локальная реализация не означает
deployment. После приёмки отдельно выполняются YDB migration и consumer label
workflow/tag cutover в `ydb-platform/ydb`. `docs/deployment.md` различает новый
install трёх таблиц и одноразовую migration из `v1.0.x`; автоматической DDL при
создании runtime нет. До внешнего cutover действующий consumer не меняется.
