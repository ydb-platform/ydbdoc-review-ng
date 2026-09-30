# Стратегия тестирования

## Unit tests рядом с функционалом

- Разработчик каждой атомарной задачи пишет unit tests её публичного поведения.
- Model, YDB и GitHub boundaries проверяются fakes без сети и платных calls.
- Negative test обязан достигать реальной production branch.
- GitHub GET retry проверяется на transient transport/5xx, а mutation failure —
  на отсутствие повтора при неопределённом результате.
- Fixture добавляется для конкретного requirement, а не ради размера матрицы.
- После изменения контракта резервного critic перед платным переводом отдельно
  запускается model-contract probe. В репозитории инструмента это
  `.github/workflows/model-contract-probe.yml`; если model secrets доступны
  только consumer-репозиторию, на исходный PR временно ставится
  `doc_model_probe`, после чего штатный `doc_translate` выполняет лишь probe.
  Он проверяет синтетическими, не содержащими документацию запросами реальный
  alternate provider и плоский ответ `corrected_markdown`, основной
  critic-editor с фактической коррекцией и независимый read-only arbiter;
  offline suite не доказывает cross-provider совместимость.

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
pending-only translation с source protected bytes, review только unresolved paths,
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
