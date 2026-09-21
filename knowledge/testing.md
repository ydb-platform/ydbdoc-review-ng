# Стратегия тестирования

## Unit tests рядом с функционалом

- Разработчик каждой атомарной задачи пишет unit tests её публичного поведения.
- Model, YDB и GitHub boundaries проверяются fakes без сети и платных calls.
- Negative test обязан достигать реальной production branch.
- Fixture добавляется для конкретного requirement, а не ради размера матрицы.

## Независимая приёмка

Независимый tester для каждой задачи:

1. Сопоставляет diff с актуальным `REQUIREMENTS_RU.md`.
2. Проверяет positive и negative user path через публичный interface.
3. Доказывает non-vacuity fixture и assertion, особенно для fenced comments,
   malformed model responses, YDB errors и GitHub side effects.
4. Запускает focused suite, static checks и релевантные regressions.
5. Возвращает PASS только для точного проверенного состояния tree.

Перед release выполняются offline end-to-end сценарии `doc_translate` и
`doc_verify`, полный non-live suite, Ruff, mypy и `git diff --check`. Нет
обязательной квоты fixtures или самостоятельных матриц без продуктового риска.

Обязательные orchestration witnesses включают: terminal job status/error на
раннем failure и success обоих workflow; отсутствие GitHub/worktree/model
effects после failed mandatory validation при разрешённых YDB audit writes;
budget gate только перед новым `doc_translate`; включение известных
`doc_verify` critic/repair costs в следующий дневной SUM; `NULL` для unknown
cost и отдельный достоверный zero-cost case. Mixed-locale PR при исчерпанном
бюджете обязан вернуть quota error при нуле direction и иных model calls.

Минимальный `doc_verify` acceptance test вручную меняет URL, path или code в
translation branch относительно authoritative source и получает rejection по
exact protected-fragment invariant. Тест не строит navigation graph и не
вызывает link resolver.
