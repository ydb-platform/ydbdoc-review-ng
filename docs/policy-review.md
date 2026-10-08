# Проверка правил документации: реализация и подключение

Добавлен самостоятельный пакет `ydbdoc_review_ng.policy_review` и локальная
команда `ydbdoc-review review`. Добавлены admission, доверенный producer, YDB audit и Docker worker; production
workflow нового режима пока не подключён. Требования и согласованная схема передвигаемого
тега находятся в [DOC_REVIEW_REQUIREMENTS_RU.md](../DOC_REVIEW_REQUIREMENTS_RU.md).

## Что реализовано

- Загрузка полного canonical policy и трёх нормативных файлов в заданном порядке.
- Проверка путей, SHA, структуры и ограничений размера snapshot.
- Формальные проверки MD009, MD032 и языка SQL fences (`yql`). Старые одинаковые
  формальные нарушения не попадают в новые findings.
- Model adapter на общем с переводчиком `deepseek-v4-flash`, с reasoning `none`.
- Денежный резерв перед каждой HTTP-попыткой, включая повтор невалидного ответа.
  Unknown cost сохраняет резерв и останавливает дальнейшие запросы.
- Строгая проверка findings: правило, приоритет, существующий файл/строка и
  точная цитата. Полная coverage правил 1–15 обязательна в semantic response.
- Явное указание неполной проверки: текстовый reviewer не подтверждает стиль
  изображений; проверка ссылок/якорей ещё не подключена.

## Локальная проверка без сети и модели

```bash
ydbdoc-review review \
  --snapshot tests/fixtures/policy_review/snapshot.json \
  --output review_report.json
```

Fixture содержит синтетическую статью и фиксированные правила из PR #55451;
его происхождение описано рядом. Успешная локальная команда имеет статус
`formal_only`, стоимость 0 и coverage `not_checked` для семантических правил.
Это завершение локального этапа, не положительный Check полного ревью.

Snapshot содержит `head_sha`, `base_sha`, `rules_sha`, список `files`
(path/before/after), maps `rules` и `context`. Значения текста не исполняются.
Снимок должен быть подготовлен доверенным caller: наличие SHA в JSON само
по себе не доказывает происхождение правил или разрешение на платный запуск.

## Граница платного адаптера

`BudgetedPolicyModel` принимает явно переданные provider credentials, transport,
`RunBudget` и обязательный recorder попыток. `RunBudget.from_environment`
требует `YDBDOC_REVIEW_MAX_RUN_COST_RUB`: пустое/некорректное значение запрещает
запуск, ноль отключает платные вызовы. Default суммы отсутствует.

Финансовый резерв рассчитывается по полному wire request с существующей в
проекте верхней оценкой input tokens через UTF-8 bytes и ограничением output
4096 tokens. Это контроль допуска запросов по конфигурации тарифов; актуальность
тарифов и соблюдение bounds provider требуется проверить до платного пилота.
Изменение тарифа провайдера или превышение его фактического usage может
привести к overrun, который сохраняется и запрещает следующие запросы.

Audit callbacks сохраняют reserve/cost вне worker до продолжения работы через
`YdbReviewStore`. Ошибка recorder запрещает дальнейшие вызовы. Ledger не хранит
model prompts/transcripts; сохраняются usage/cost и служебная версия. Отдельный
finalizer восстанавливает отменённые запуски из ledger. Fake callbacks используются
только в offline tests; реальные транзакции проверяет image build smoke.

CLI первого этапа не включает платный режим и не читает model credentials.
Это предотвращает обход ещё не подключённого admission gate через локальную команду.

## Лимит и ожидание новых коммитов

В репозитории **ydb-platform/ydb** открыть
`Settings → Secrets and variables → Actions → Variables → New repository variable`.
Имя: `YDBDOC_REVIEW_MAX_RUN_COST_RUB`; значение: желаемая сумма RUB на запуск,
например `50` или `12.50`. Эти числа — примеры, не значения по умолчанию.
`0` отключает платные вызовы; отсутствующее/невалидное значение запрещает их.
Лимит проверяется перед каждой платной попыткой. Сумма меняется без правки CI.
Consumer должен передавать `${{ vars.YDBDOC_REVIEW_MAX_RUN_COST_RUB }}` в action
и одноимённое окружение worker; автоматического доступа к Variables другой repo нет.

Добавлен `policy_review.debounce.wait_for_quiet_head`: лёгкое ожидание 300 секунд
с проверкой актуального SHA и состояния PR каждые 15 секунд и на границе запуска.
Смена SHA возвращает `superseded`; новое событие начинает свои 300 секунд.
Серия коммитов в t=0/60/120 даёт готовность только последнего head не ранее t=420.
Закрытие/draft останавливают ожидание, сбой API не разрешает старт. Интервал
измеряется monotonic clock от обработки события, не от timestamp коммита.
Медленный runner может увеличить фактическую задержку.

Механизм подключён к GitHub admission и producer; consumer workflow подготовлен
как шаблон, но ещё не установлен в ydb. Caller обязан проверить допуск после ожидания, обеспечить
`concurrency` по repository/PR и постоянную lease между платными worker,
повторно сверить SHA перед вызовом модели и перед публикацией. Сам helper не
предоставляет разрешение, не дедуплицирует события и не блокирует другие процессы.
Нерелевантные метки не должны входить в группу отмены. Пяти минут ожидания
недостаточно, чтобы исключить новый push после начала платного ревью: такую
старую попытку потребуется остановить и сохранить её расходы.

## Оставшиеся этапы

1. Подтвердить Docker build и финансовые транзакции на временной YDB.
2. Выпустить проверенный image/action, применить новые audit tables и подключить
   consumer workflow в ydb.
3. Добавить полные формальные проверки links/includes/TOC, inline comments и suggestions.
4. Провести небольшой платный пилот после утверждения источника правил.

Платные вызовы и публикация замечаний в ydb пока не выполнялись.

## Admission, snapshot и публикация

`commands gate` проверяет автора через collaborators API и оператора метки через
repository permissions API. `ok-to-test` сохраняется на конкретный head SHA;
удаление метки другим CI не аннулирует разрешение. `doc_review` не обходит gate.
Командная метка принимается от write/maintain/admin и снимается доверенным caller.
Actor association, PR body, инструкции и расположение fork не являются допуском.

До `concurrency` атомарно сохраняется ticket события в YDB. Повтор того же head
не отменяет существующую работу. `gate` не вызывает модель. Новый head внешнего
автора без approval останавливает старую попытку, но сам остаётся в ожидании.
После debounce повторно проверяются author/operator/head и approval.

Producer читает все pages списка файлов (до лимита API 3000), сверяет количество,
читает полные Markdown/YFM тексты; before берёт с merge base, rules — с base SHA
или явно утверждённого `YDBDOC_REVIEW_RULES_SHA`. Symlink/submodule не принимаются.
Связанные Markdown includes/страницы читаются как данные, без внешних URL и выполнения
кода. Неоднозначный, слишком большой или неполный snapshot прекращает запуск.

После atomic claim только владелец lease данного PR начинает платные попытки.
Перед каждой попыткой перепроверяются текущий head/status и ownership. Отмена
сохраняет reserve; неизвестная стоимость не освобождает lease автоматически.
Publisher получает отдельный write token, создаёт Check именно на проверенный head
и обновляет собственный общий комментарий. Чужой скопированный marker игнорируется.
Для информационного пилота неполная проверка даёт neutral, а не clean/success.
Inline review comments и GitHub suggestions пока не реализованы.

## Подключение

1. Инициализировать только новые audit tables из `docs/doc-review-schema.sql`:
   `python -m ydbdoc_review_ng.policy_review.commands init-schema --file docs/doc-review-schema.sql`.
   Запускать из доверенного checkout с доступом к audit YDB; PR worker не выполняет DDL.
2. Выполнить workflow `Build documentation policy reviewer` в main. Он собирает
   worker с hash-locked зависимостями из внутреннего mirror, проверяет offline CLI
   и транзакции на одноразовой anonymous /local YDB; затем публикует image в GHCR.
   Этот workflow не получает credentials модели и не может сделать платный вызов.
3. После успеха перенести `review-image.json` из build artifact в `docker/review-image.json`,
   опубликовать этот commit в main и сдвинуть `v1.0.1` на него.
   До этого image manifest содержит null, production worker не выпускается.
   Image должен быть доступен consumer repo для pull; для публичного YDB удобен public GHCR package.
4. PR workflow и finalizer должны находиться в `.github/workflows/` репозитория
   **ydb-platform/ydb**. Согласованные копии хранятся в `examples/` репозитория кода.
   Ссылка на action остаётся по `v1.0.1`; image digest меняется внутри action.
5. Как и `doc_translate`, consumer использует secrets
   `YANDEX_CLOUD_API_KEY_DOC_REVIEW`, `YANDEX_CLOUD_FOLDER_DOC_REVIEW`, `YDB_SA_KEY`
   и нативный `${{ github.token }}`. `YDB_TOKEN`, `YDB_ENDPOINT` и `YDB_DATABASE`
   остаются необязательными overrides: при пустых значениях применяется тот же
   default audit endpoint/database и service account key, что у переводчика.
   Бюджет передаётся из `vars.YDBDOC_REVIEW_MAX_RUN_COST_RUB`.
   Нативный token применяется только вне Docker: metadata read для проверки
   collaborators/permission, checks write, pull-requests write и issues write
   для публикации и меток; finalizer также имеет actions read.
   Отдельный секрет `YDB_GH_TOKEN` в ydb не требуется.
6. До слияния правил в main явно согласовать immutable `YDBDOC_REVIEW_RULES_SHA`.
   Без этой настройки missing canonical rules приводит к остановке без модели.

Gate, run и finalizer используют один trusted action. Controller до Docker проверяет
фактический action SHA: downloaded code должен совпасть с Git tree разрешённого tag.
Если tag сдвинут между скачиванием и разрешением версии, выполнение останавливается.
Worker image выбирается по digest; его OCI revision сверяется с release manifest.
В audit сохраняются action SHA, runtime revision, image digest и pricing version.
Доставка обновлений consumer workflow не меняет.

## Отмена и зависшие запуски

Обычная отмена launcher останавливает Docker и затем восстанавливает результат
из YDB. Финализирующий workflow работает после завершения Actions run, включая
уничтожение runner. Он проверяет статус/name/event удалённого run и не вызывает модель.
Без незавершённых paid reservations lease освобождается. При reserved/unknown
PR остаётся заблокированным до сверки реальной стоимости: автоматическое присвоение
нулевой цены или освобождение по TTL могло бы нарушить финансовый контракт.
Сверка расходов после timeout пока является процедурой сопровождающего.
Не выдавать такую остановку за успешное ревью.

## Разделение GitHub credentials

Все GitHub операции используют нативный Actions token вне Docker, по примеру
`doc_translate`. Checks API использует отдельный host transport с checks write.
[GitHub Checks API](https://docs.github.com/en/rest/checks/runs#create-a-check-run).
Внутреннее разделение read/admission/comment/checks transport сохраняется.

В Docker не передаётся ни один GitHub token. Worker читает только публичный
`GET /repos/ydb-platform/ydb/pulls/<number>` для проверки актуального head/status.
Остальной snapshot уже подготовлен доверенным controller. Ошибка или rate limit
публичного API прекращают работу до очередного платного запроса. Аутентифицированные
polling/debounce, membership, comment/label и Checks операции остаются вне контейнера.

## Размещение CI и включение

Workflow сборки `Build documentation policy reviewer` живёт в `ydbdoc-review-ng`:
он собирает код worker и не реагирует на PR документации. Два workflows, которые
обрабатывают PR документации (`ydbdoc-policy-review.yml` и finalizer), устанавливаются
в `ydb-platform/ydb/.github/workflows/`. Внешний action вызывается по стабильному тегу.

Переменная `YDBDOC_REVIEW_ENABLED` в **ydb-platform/ydb** должна иметь ровно `true`
для запуска jobs. Пока image build/YDB smoke, production audit schema и stable tag
не готовы, переменная отсутствует или равна `false`: новые jobs пропускаются до
скачивания action, доступа к YDB и модели. Это позволяет опубликовать CI сейчас
и включить его без следующей правки workflow. Бюджет 5000 RUB сам по себе не включает CI.
Не менять этот переключатель во время активного платного запуска: дать finalizer
сохранить его аудит. Для отключения платных запусков использовать бюджет `0`.

Workflows предложены в ydb: [draft PR](https://github.com/ydb-platform/ydb/pull/55586). Прямая запись в main
запрещена repository rules; CI ещё не установлен и не включён.

## Версионный тег по примеру doc_translate

Проверенный consumer переводчика ссылается на `.github/actions/doc-review@v1.0.1`.
Новый action использует `.github/actions/policy-review@v1.0.1`: тот же тег, что
переводчик. Все три вызова (gate/run/finalize) используют одну версию.
Имя v1.0.1 остаётся постоянным; перенос этого тега при обновлении и аудит фактического
SHA/digest соответствуют ранее согласованной доставке без изменений CI.
Существующий тег переносится после успешного image build и проверки финансовых
транзакций. Перенос обновляет также action переводчика: перед выпуском проверить
его совместимость. Сейчас тег не перенесён; review остаётся отключённым.
