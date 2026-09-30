# Стратегия тестирования

Новый PR review contract утверждён 2026-09-30, но ещё не реализован и не
provider-validated. Описанные ниже новые acceptance witnesses должны быть
добавлены в реализации; этот documentation change их не исполнял.

## Unit tests рядом с функционалом

- Разработчик каждой атомарной задачи пишет unit tests её публичного поведения.
- Model, YDB и GitHub boundaries проверяются fakes без сети и платных calls.
- Negative test обязан достигать реальной production branch.
- GitHub GET retry проверяется на transient transport/5xx, а mutation failure —
  на отсутствие повтора при неопределённом результате.
- Fixture добавляется для конкретного requirement, а не ради размера матрицы.
- Новый live model-contract probe обязан использовать production context
  builders/parsers: translator segment map, полный multi-file editor с
  исправлением двух файлов и межфайлового дефекта, validation/full build и
  независимый полный PR arbiter. Полный pinned glossary включается в оба calls.
  Проверяются все реально используемые providers; optional fallback только если
  этот путь сохранён. Старые synthetic excerpt probes исторические и недостаточны.
  Offline suite не доказывает provider capacity или semantic correctness.

## Независимая приёмка

Независимый tester для каждой задачи:

1. Сопоставляет diff с актуальным `REQUIREMENTS_RU.md`.
2. Проверяет positive и negative user path через публичный interface.
3. Доказывает non-vacuity fixture и assertion, особенно для fenced comments,
   malformed model responses, YDB errors и GitHub side effects.
4. Запускает focused suite, static checks и релевантные regressions.
5. Возвращает PASS только для точного проверенного состояния tree.

Перед release выполняются offline end-to-end сценарии `doc_translate`,
`doc_verify` и `doc_continue`, полный non-live suite, Ruff, mypy и `git diff --check`. Нет
обязательной квоты fixtures или самостоятельных матриц без продуктового риска.

Обязательные orchestration witnesses включают: terminal job status/error на
раннем failure и success обоих workflow; отсутствие GitHub/worktree/model
effects после failed mandatory validation при разрешённых YDB audit writes;
budget gate только перед новым `doc_translate`; включение известных
`doc_verify` critic-editor costs в следующий дневной SUM; `NULL` для unknown
cost и отдельный достоверный zero-cost case. Mixed-locale PR при исчерпанном
бюджете обязан вернуть quota error при нуле direction и иных model calls.

Минимальный `doc_verify` acceptance test вручную меняет URL, path или code в
translation branch относительно authoritative source и получает rejection по
exact protected-fragment invariant. Тест не строит navigation graph и не
вызывает link resolver.

`tests/e2e/test_offline_continue.py` вызывает CLI через реальный `create_runtime`,
заменяя только GitHub/model HTTP и YDB executor. Witnesses: direction-only retry,
pending-only translation с source protected bytes, любой review полного PR/glossary,
один verdict, GREEN close, неизменный expiry после повторного RED. Missing, empty,
unauthorized или поздний comment, expired и stale checkpoint дают exit 1,
terminal audit и ноль model/GitHub mutation effects. CLI и shell action отдельно
проверяют PR-only continue и RED exit status; bootstrap читает consumer YAML
template после подстановки тестового immutable SHA.

Installed smoke копируется вне checkout вместе с `_runtime_services.py` и
исполняется Python из окружения с установленным wheel, без `PYTHONPATH=src`.
Он выполняет translate, RED verify и успешный continue с закрытием checkpoint,
проверяет расположение импортированного пакета внутри venv и запрещает сеть.

Регрессии технической коррекции проверяют ровно два TRANSLATE вызова на
неделимый невалидный chunk, реальные дочерние source ranges после split и
отсутствие повторов успешного соседа. ScriptedModels не повторяет последний
ответ автоматически при исчерпании сценария. Continuation fixtures сохраняют
невалидность до завершения коррекции/деления, затем явно меняют поведение для
продолжения. Cost assertions учитывают фактическое новое число attempts.
Тестовый пакет имеет `tests/__init__.py`, чтобы установленный YDB SDK со своим
пакетом `tests` не перехватывал импорты проектных тестов.

В контейнере без reaping PID 1 проверки kill process group следует запускать
под init/subreaper: завершённые orphan grandchildren иначе остаются zombies и
`kill(pid, 0)` ошибочно выглядит как продолжающийся процесс. Это настройка
тестового окружения; контракт timeout wrapper не ослабляется.
# Translation-plan tests (2026-09-30)

`tests/unit/test_translation_plan.py` is intentionally separate from runtime,
model and publication tests. It treats the source PR inventory as a closed set
and checks that every path receives exactly one explicit disposition before
execution. The matrix covers path kinds, Markdown status/operation pairs,
old/new rename boundaries, complete pairs, target-side/outside-locale changes,
all supported TOC names, unsupported TOC statuses and localized kinds,
metadata-only direction, output collisions, canonical ordering, immutability,
fixed/final reconciliation and the exact four-file shape of PR #50839.

The #50839 regression also runs the production composition end to end with
recorded complete `toc_i.yaml` fixtures. It asserts exact published target
bytes, including preservation of target-only entries. Planner unit tests bind
the complete expected TOC digest and reject a merely non-null dummy file.

Unsupported matrix cells are expected failures, not missing tests. Structural
TOC delta/no-op proofs, redirects, assets and persisted plan replay remain the
next executor layer described in `docs/translation-plan.md`.

## Полный PR semantic witness #50839 / #54590

Fixture `tests/fixtures/quality/pr50839_pr54590/` должен содержать полные
RU/EN `blobdepot.md`, `blobdepot_decommit.md`, `index.md`, `toc_i.yaml`
под `ydb/docs/{ru,en}/core/maintenance/manual/`, обе полные glossary, original PR
inventories и PROVENANCE/manifest с SHA-256 всех bytes. Исходный PR:
base `1705aa4cea8caaf8c715b368f57ac7317975af83`,
head `12c8b806dc4560ff7322cd7464dc2372a1b614c6`,
merge `30d5bd68e2f97cbb41aaf782149435e4b9c1bdbc`.
Фактический authoritative source snapshot перевода:
`9191121586f4d8061414d597cdcc2f4ec8d42d20`; bad target head:
`b7b27bcf34d9761f0311011cd3fba051c05cd4ca`.
Original head нельзя подменять authoritative snapshot.
Pinned witness glossary blobs: RU `313f9ba9ca5f7e1188b9e3bf2233fe4da2ac53b2`
(146214 bytes), EN `b9792fee56ad091fae76d29185759d4b99cb9c45`
(79218 bytes), всего 225432 UTF-8 bytes. Исторические 48000-character limits
и excerpt probes не доказывают новый контракт.

Witness checks:

- Fragmentation BlobDepot/Blobovnica/blobber и смешение BlobStorage должны
  обнаруживаться между файлами даже без exact glossary mapping.
- Сохраняются bytes `{{ ydb-name }}`, исправляется грамматика вокруг template.
  `BS monitoring page\_CONTROLLER` не заменяет цельный `BS_CONTROLLER`.
  Склейка `--storage-pool-namein` исправляется без изменения параметра.
- Читаемое inline-code оформление technical literals проверяется по source/
  candidate. Старый EN baseline не подмешивается в model input; потерю исходных
  backticks доказывает отдельный source fixture с настоящими backticks.
- H1/index/TOC согласованы по сущностям и запланированным операциям. Valid
  target-only navigation сохраняется. Искусственное равенство всех множеств
  index/TOC links запрещено: некоторые ссылки закономерно находятся вне TOC.
- Positive offline replay использует production context builder/schema/parser,
  atomic apply, validation/build и publication fake. Fake editor возвращает
  независимо проверенные полные файлы, fake arbiter проверяет exact built bytes,
  все четыре paths и оба полных glossary, включая tail markers. Negative replay
  оставляет старые defects и получает RED.
- Runtime отвергает missing/unknown/duplicate/malformed/partial file maps,
  binary/deletion/traversal/out-of-scope entries, пустой ответ/checked set,
  missing arbiter, невалидный RED, digest mismatch и unvalidated metadata.
  Editor changes и no-op не подменяют полный arbiter.
- Verified input/context/output capacity резервируется для точных serialized
  requests и полных outputs. Unknown capability, oversize, failed validation/
  build или moved head дают ноль publication calls с terminal audit/cost.
  Fallback не сокращает контекст, glossary или outputs.
- Любой continuation review вновь включает весь PR, в том числе accepted
  documents. Старые chunk-review checkpoints fail closed; новые связывают
  contract version и source/candidate/glossary manifests/digests, exact head,
  TTL и CAS. PR attempts имеют NULL target_path и аудируемый context identity.

Scripted offline GREEN доказывает только orchestration. Semantic acceptance
требует real-provider full-context probe и независимого review полных outputs.
Исторический TOC golden доказывает сохранение target-only entries и planner
shape, но не правильность перевода label `Group Decommissioning` или всего PR.
