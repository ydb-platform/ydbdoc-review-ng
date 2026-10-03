# Архитектурные инварианты

Канон: `REQUIREMENTS_RU.md`.

## Поток

`label → snapshot → (budget) → direction → Python scope → translate/TOC/ops →
draft commit → critic tool-workspace (patch+re-read → reviewed push) →
arbiter judge-only (GREEN/YELLOW/RED) → QA comment`.

| Режим | Translator | Budget gate | Ветка |
|---|---|---|---|
| `doc_translate` | да | да | удалить старую, создать чистую |
| `doc_verify` | нет | нет | та же; critic пушит правки |
| `doc_continue` | только pending / direction | нет | та же; + operator context |

## Владение

- **Python:** inventory, Git-операции, dependency closure, protected fragments,
  TOC structural delta, publication, checkpoints.
- **DeepSeek:** нужен ли перевод + направление; whole-file prose; TOC strings;
  critic tool edits (patches); arbiter verdict/findings only.
- Модель **не** назначает per-file semantic actions.

## Публикация и verdict

- Любой собранный UTF-8 файл публикуется. Diagnostics ≠ gate.
- GREEN / YELLOW = успех, checkpoint закрыт.
- RED = неуспех, checkpoint открыт (`/ydbdoc continue` или ручная правка +
  `doc_verify`).
- YELLOW = мелкие проблемы (например нет EN Wikipedia, термины).
- Build/CI не влияют на semantic verdict.
