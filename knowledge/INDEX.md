# Банк знаний ydbdoc-review-ng

Для переводчика при конфликте побеждает `REQUIREMENTS_RU.md`. Проект
отдельного режима проверки правил описан в `DOC_REVIEW_REQUIREMENTS_RU.md`;
он не изменяет действующий контракт переводчика. В разделе нового режима
подтверждённые факты отделены от рекомендаций и открытых вопросов.

- `architecture.md` — режимы и инварианты (thin pipeline)
- `translation-algorithm.md` — scope, thin translate, gates, arbiter
- `model-api.md` — DeepSeek contracts
- `testing.md` — TDD и witnesses
- `delivery.md` — commits в `main`
- `current-status.md` — что согласовано и где код
- `debt-map.md` — исторические срезы выравнивания
- `blobdepot-golden-plan.md` — P2 BlobDepot offline golden (done)
- `tool-using-critic-plan.md` — **архив** (tool-critic снят с production)
- `p0-deepseek-tools-probe.md` — исторический live tools probe

Не хранить секреты, полные transcripts, черновики агентов.

- [doc-review/INDEX.md](doc-review/INDEX.md) — проект проверки правил, исследованные источники и открытые решения.
