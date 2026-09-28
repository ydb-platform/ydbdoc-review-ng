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
  Он проверяет синтетическим, не содержащим документацию запросом реальный
  alternate provider и плоский ответ `corrected_markdown`; offline suite не
  доказывает cross-provider совместимость.

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
