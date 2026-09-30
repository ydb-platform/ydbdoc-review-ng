# Банк знаний ydbdoc-review-ng

Этот каталог различает утверждённые требования и проверенные факты реализации.
При конфликте приоритет имеет `REQUIREMENTS_RU.md`. Полный PR review утверждён
2026-09-30, но ещё не реализован и не provider-validated; исторические probes
не подтверждают его. Точное состояние и граница исторических записей находятся
в `current-status.md`.

- `architecture.md`: границы системы и неизменяемые инварианты.
- `translation-algorithm.md`: whole-file/source-only translation с внутренними
  structural chunks и отдельный полный PR critic/runtime/arbiter contract.
- `model-api.md`: проверенные возможности и ограничения моделей Yandex Cloud.
- `testing.md`: правила тестирования и независимой приёмки.
- `delivery.md`: локальный конвейер разработки и коммитов.
- `current-status.md`: возобновляемый handoff текущей реализации и GitHub-запуска.

Не хранить здесь секреты, полные model transcripts, временные планы агентов или
неподтверждённые идеи.
