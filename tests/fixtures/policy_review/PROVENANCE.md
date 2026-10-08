# Источники fixtures проверки правил

Нормативные файлы в snapshot.json прочитаны из публичного ydb PR #55451
на SHA `df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15`:
[canonical policy](https://github.com/ydb-platform/ydb/blob/df4ae35bf5c3c5a53162c8b456c35b0a2c7f6e15/ydb/docs/.ruler/DOCUMENTATION_POLICY.md)
и GENERAL_RULES.md, FORMAT_RULES.md, DOCUMENTATION_RULES.md в той же директории.
Это фиксированная тестовая копия, не канонический источник правил runtime.

Проверяемая статья, head/base SHA — синтетические тестовые данные.
Этот snapshot не описывает реальный PR. В первом этапе runtime доверяет
подготовленному snapshot; проверку происхождения данных выполняет будущий admission adapter.
