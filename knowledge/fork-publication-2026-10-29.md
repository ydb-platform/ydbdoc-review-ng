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

Публикация main/tag и повторный запуск #53925 пока не выполнены:
api.github.com не разрешается по DNS, включая запрос вне sandbox.
