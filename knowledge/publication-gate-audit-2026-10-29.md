# Аудит ограничений публикации перевода, 29 октября 2026

Требование пользователя: переводим техническую документацию. Регистр букв,
оформление и отдельные слова другого языка не должны отбрасывать весь файл.
Причина аудита: в PR53925 перевод benchmarks.md был готов, но второй успешный
ответ модели отвергнут из-за `into` вместо `Into` в заголовке. CI40838344716
создал PR57416 без этого файла и выставил RED. Подробности и audit job:
[pair-classification.md](pair-classification.md).

## Удалено

| Условие / преобразование | Причина |
|---|---|
| `en_heading_title_case` | Sentence case и предлог со строчной буквы не делают перевод непригодным |
| `source_locale_echo` (три кириллические буквы) | Имена, цитаты, интерфейсы и код могут содержать кириллицу |
| `heading_blank_lines` | Пустые строки не должны блокировать целый файл |
| `unlabeled_fence_opener` | Fence без языка является допустимым Markdown |
| `split_backtick_identifiers` | Последовательность может быть буквальным примером синтаксиса |
| Глобальное исправление split-backtick | Меняло содержимое примеров кода |
| Глобальное склеивание prose-строк | Склеивало YAML front matter, строки tilde fences и Markdown hard breaks |
| Нормализация пробелов списков и пустых строк | Не нужна для перевода; сохраняем ответ модели |
| Отдельный regex пустых ссылок в document validator | Структурная проверка и так обнаруживает добавленные ссылки; regex дублировал её |

Проверка длинного дословного source echo в legacy document path остаётся
мягкой: одна корректировка, затем диагностика. Она не является запретом
публикации по алфавиту.

## Сохранено

- Невалидный UTF-8 и отсутствие настоящего include в итоговом дереве.
- Контракты ответа (в том числе JSON ID map для TOC), плейсхолдеры в путях,
  которые их используют, и проверки структурной целостности этих путей.
- Уникальность и непересечение surgical replacements, допустимые пути,
  snapshot SHA и защита от записи поверх изменившегося состояния.
- Бюджеты запросов и лимиты зависимостей.
- Итоговая семантическая оценка arbiter. Снятие style gates не означает,
  что любой ответ автоматически получает GREEN.

В thin production path структурная проверка уже была диагностической;
этот аудит не превращает её в новый hard gate и не меняет её политику.

## Include в примерах

`include_targets` использует markdown-it-py для границ Markdown blocks,
контейнеров списков/цитат и inline code. Link parsing отключён, чтобы YFM
include оставался текстом для существующего извлечения destination.
Примеры кода не становятся зависимостями. Настоящие include внутри списков,
после экранированных backticks и между разными абзацами остаются проверяемыми.
Парсер используется только для обнаружения зависимостей, не для переписывания
документа. Новая runtime dependency объявлена в pyproject.toml.

## Проверка

- Записанный ответ модели benchmarks.md принят без изменения регистра.
- Runtime regression сохраняет YAML, hard breaks, tilde fence и literal
  split-backtick с одним model call.
- Тесты сохранения spacing, проверки UTF-8 и include с Markdown containers.
- 87 focused tests passed; Ruff изменённых translation modules/tests и mypy
  gates/document проходят. В runtime_content остаются две прежние F841.
- Итоговый focused run с package bootstrap: 100 passed. Full: 2390 passed,
  270 failed, 7 deselected; 269 failures уже были в baseline, один новый был
  точным сравнением списка dependencies и исправлен (bootstrap rerun passed).
  Три прежних runtime tests теперь проходят. Полный suite не является зелёным.
- Mypy runtime_content сообщает 26 ошибок; текущая проверка не является
  подтверждением чистоты всего runtime. Scope gates/document чистый.
- HTML-примеры и весь YFM не заменяются этим CommonMark scanner. Это не
  полный YFM parser; аудит не меняет прежнюю обработку HTML как текста.

## Поставка

Независимое review: blockers нет, 87 focused tests подтверждены reviewer.
Код f67e360 опубликован в main; v1.0.1 передвинут с ad56276 на f67e360.
На PR53925 заново поставлена метка doc_translate:
https://github.com/ydb-platform/ydb/actions/runs/40842548202 .
При записи этой заметки run in_progress; успешный итог ещё не подтверждён.

### Результат CI40842548202

Запуск завершился success 2026-10-29 08:46:28 UTC, action SHA f67e360.
Создан PR57422, head b60c02b2e22db0389a8555fc30a4c2e7fa19758b, шесть файлов.
Arbiter GREEN: https://github.com/ydb-platform/ydb/pull/57422#issuecomment-6492131097 .
benchmarks.md классифицирован no_translation (08:40:32, document_index55),
поэтому этот run не является live witness принятия перевода benchmarks новым gate.

Следующий PR-check40844568834 упал при сборке документации. MD022 требует
пустую строку между соседними заголовками в RU contributor/datashard-locks-and-change-visibility.md,
строки137–138: broken-locks / whole-shard-locks. Удаление нормализации spacing
не ломает translator job, но допускает результат, отвергаемый downstream build.
GREEN arbiter не означает merge-ready. Следующий вариант исправления:
узкая нормализация границ заголовков вне кода, без возвращения отбрасывания
всего файла и без глобальной склейки строк. Пока не реализовано.
