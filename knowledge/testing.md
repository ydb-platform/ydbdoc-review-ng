# Стратегия тестирования

## Цикл разработки

- Каждое изменение публичного поведения начинается с failing regression test.
- Фокусные тесты запускаются после каждой атомарной правки. Полный suite,
  Ruff, mypy, `git diff --check`, wheel/sdist и installed smoke выполняются один
  раз перед release.
- Model, YDB и GitHub boundaries в unit/integration tests заменяются fakes. Live
  tests не входят в обычный offline suite.
- Независимый tester проверяет diff против `REQUIREMENTS_RU.md`, positive и
  negative paths, non-vacuity assertions и отсутствие обхода production branch.
- Mutation testing, property generators, shrinkers и искусственные quota matrices без
  отдельного требования не добавляются.

## Обязательные witnesses

- Whole-file translator получает один полный source-файл. Дополнительного
  model-request limit и внутридокументного chunking нет.
- Strict translator parser отклоняет missing, unknown и duplicate IDs; одна
  correction покрыта отдельно.
- Любой технически собранный UTF-8 candidate публикуется даже при
  Markdown/YFM/YAML/TOC/link/protected diagnostics. Тесты не должны требовать
  отказ от такой публикации.
- Critic parser покрыт positive и negative tests: exact `files`, every requested path
  once, no unknown paths/duplicates/non-string/invalid UTF-8. Пустой textual scope
  требует `{"files": {}}`.
- Critic chunk success сразу создаёт commit. Ошибка другого чанка не откатывает
  его. Первый critic success после нулевого translator result создаёт branch/commit/PR.
- Arbiter chunks пересчитываются по финальным файлам. Worst verdict побеждает;
  GREEN возможен только когда все чанки GREEN.
- Missing/unreviewed target допускает `null` line/snippet. Existing reviewed target
  требует exact line и searchable snippet.
- Deterministic-only PR публикует initial commit, запускает manifest-only critic
  и arbiter calls и берёт итоговый verdict у арбитра.
- При нуле commits после translator и critic пустой PR не создаётся. RED report
  и checkpoint с `target_sha = null` привязываются к source PR.
- `doc_continue` после direction success не останавливается; pending translations
  имеют приоритет над review findings, после чего вся группа повторно
  проходит critic и arbiter.
- Build и CI не вызываются и не читаются для semantic verdict.

## Release gate и live validation

После всех атомарных задач один раз выполняются полный offline suite, static
checks, package build и installed CLI smoke для translate/verify/continue. После этого
независимые tester и reviewer проверяют exact tree.

`doc_model_probe` остаётся отдельным исследовательским тестом. Он не является
production translation flow и не публикует документы.
