# Публикация переводов в fork

По согласованию с пользователем action публикует Git-объекты и ветки
`translation/pr-N` в `sintjuri/ydb`, PR открывает в `ydb-platform/ydb`.
Source/base остаются upstream. Перед первой записью проверяется принадлежность
fork к сети upstream. Read cache различает repository и SHA.

Verify и continue проверяют head repository и читают опубликованный target из
fork. Reset удаляет только fork branch. Старые upstream ветки не мигрируются:
для старого checkpoint нужен свежий doc_translate.

Action задаёт `YDBDOC_PUBLICATION_REPOSITORY=sintjuri/ydb` во всех режимах.
При прямом запуске CLI обязательно задать ту же переменную: generic runtime
без неё сохраняет старый same-repository default.

Проверки: 92 focused tests passed; полный suite 2369 passed, 272 failed,
7 deselected. Набор failed совпадает с baseline до изменения. Независимое
review не нашло blockers в action path. Новые tests покрывают Git writes,
base/target reads, fork identity, PR head, reset, verify и continue.

Код 2094e43 опубликован в main и v1.0.1. Перезапуск #53925:
https://github.com/ydb-platform/ydb/actions/runs/40821783094 (in_progress).

В consumer обнаружены дополнительные зависимости: github.token ограничен
upstream, поиск translation PR использует upstream owner. Исправление:
https://github.com/ydb-platform/ydb/pull/57400, ветка только в sintjuri/ydb.
Workflow использует существующий секрет YDBDOC_PUSH_PAT. Его фактические права
нельзя прочитать через API: нужны Contents write в fork и PR/issues write
upstream. До слияния consumer PR label-workflow не сможет писать в fork.

Системный DNS зависает, dig возвращает корректный адрес. Для доставки
использован curl --resolve с обычной проверкой TLS, без изменения DNS системы.
Ruff/mypy не полностью чистые: оставшиеся диагностические сообщения в runtime
требуют отдельного сравнения с baseline; полный suite не объявлен зелёным.

Причина завершения CI: [разбор #53925](pr53925-run-40821783094.md).
