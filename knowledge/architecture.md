# Архитектурные инварианты

Канон: `REQUIREMENTS_RU.md`.

## Поток

`label → snapshot → (budget) → direction → Python scope → thin translate/TOC/ops
→ publication gates → publish reviewed → arbiter (GREEN/YELLOW/RED) → QA comment`.

| Режим | Translator | Budget gate | Ветка |
|---|---|---|---|
| `doc_translate` | да (thin) | да | удалить старую, создать чистую |
| `doc_verify` | нет | нет | та же; gates + arbiter |
| `doc_continue` | только pending / direction | нет | та же; + operator context |

## Владение

- **Python:** inventory, Git-операции, dependency closure, TOC structural delta,
  publication gates, publication, checkpoints.
- **DeepSeek:** нужен ли перевод + направление; thin whole-file prose; TOC
  strings; arbiter verdict/findings only.
- Модель **не** назначает per-file semantic actions.
- Tool-using critic **не** в production path.

## Публикация и verdict

- В ветку только файлы, прошедшие publication gates. Полу-EN запрещён.
- GREEN / YELLOW = успех, checkpoint закрыт.
- RED = неуспех, checkpoint открыт (`/ydbdoc continue` или ручная правка +
  `doc_verify`).
- YELLOW = мелкие проблемы (например нет EN Wikipedia, термины).
- Build/CI не влияют на semantic verdict.
