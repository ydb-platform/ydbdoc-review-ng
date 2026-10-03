# Стратегия тестирования

- Сначала failing regression, потом код; focused suite на задачу.
- Полный non-live suite, Ruff, mypy, `git diff --check`, package smoke —
  один раз перед release.
- Model/YDB/GitHub → fakes. Mutation testing без явной просьбы не делать.

## Обязательные witnesses

- Direction не задаёт per-file actions; Python зеркалит Git.
- Dependency: A→A1 без target → A1 в группе.
- Whole-file translator, без внутридокументного chunking.
- Собранный UTF-8 всегда публикуется.
- TOC delta: apply, идемпотентность, новый TOC только из entries PR.
- Critic success → сразу commit reviewed workspace; первый success может создать PR.
- Critic tools: patch без covering re-read → RED; turn budget → RED;
  stub-model integration offline (см. `tool-using-critic-plan.md` TDD list).
- Arbiter worst-verdict; YELLOW закрывает checkpoint; RED открывает; no repair loop.
- Ноль commits после translator+critic → нет пустого PR, RED в source PR.
- Continue: operator context; pending раньше review; потом полный re-review.
- BlobDepot golden (`tests/golden/test_blobdepot_golden.py`): atoms, presentation
  map, critic-unavailable → RED; без сети и без живой модели.
- Optional live tool smoke: `YDBDOC_LIVE=1` + model creds; default deselected.

`doc_model_probe` не production translate.
