# Конвейер разработки

1. Из канонических требований выделяется одна атомарная задача.
2. Разработчик сначала пишет failing unit test, затем минимальную реализацию.
3. Независимый tester проверяет публичное поведение, non-vacuity и regression.
4. Конкретный FAIL возвращается как bounded remediation.
5. PASS фиксируется отдельным task commit; перед релизом выполняются полный
   non-live suite, static checks, package build и installed smoke.

`v1.0.x` поставляет `doc_translate` и `doc_verify`. История разработки,
временные отчёты и paid-model transcripts не входят в публичный репозиторий.

Возврат исходного `doc_continue` является отдельным release scope. Пока он не
реализован и не прошёл собственные acceptance tests, новый action не принимает
этот mode, а production workflow продолжения остаётся на прежней версии action.
