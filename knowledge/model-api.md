# Модели и response contracts

## Production model

Все production calls используют DeepSeek V4 Flash: direction, простая
проверка необходимости перевода, translator, TOC strings, critic и arbiter.
YandexGPT и model fallback в production flow не используются. Единственное
исключение — специальный `doc_model_probe`, который не переводит и не публикует
документы.

Контекстное окно DeepSeek считается равным 1 048 576 токенов. Искусственных
пределов ответа 8 000 или 2 000 нет. Для budget каждый UTF-8 byte полного
сериализованного wire request и ожидаемого полного JSON-response считается одним
токеном. `max_tokens` равен 1 048 576 минус размер полного wire request в UTF-8
bytes; ожидаемый полный JSON-response должен помещаться в остаток. Отдельный
сетевой tokenizer call не выполняется. Упаковка critic и arbiter использует то
же правило.

## Контракты

- Direction и простая классификация возвращают strict JSON. Git inventory и
  статусы файлов остаются authoritative Python data.
- Translator возвращает точную UTF-8 JSON-карту всех запрошенных
  prose segment IDs. Unknown, duplicate и missing IDs отклоняют response, потому
  что без карты runtime не может собрать файл.
- Critic возвращает только `{"files": {"target/path": "complete UTF-8 content"}}`.
  Каждый запрошенный path должен присутствовать ровно один раз. Unknown paths,
  duplicate keys, non-string values и invalid UTF-8 запрещены. При нуле
  текстовых пар ожидается `{"files": {}}`.
- Arbiter возвращает только `verdict` и `findings`. GREEN требует пустого
  `findings`; YELLOW и RED требуют findings. Для существующего и проверенного
  target нужны exact line и searchable snippet. Для missing или технически
  unreviewed target они равны `null`.

Точные живые prompts critic и arbiter хранятся в `REQUIREMENTS_RU.md` и packaged prompt
files. Их можно менять при отладке, не меняя orchestration.

## Повторы и аудит

Critic и arbiter повторяются ровно один раз при provider error или
malformed response. Неполный critic response не применяется. Ошибка одного
чанка не останавливает остальные.

Каждая attempt аудируется с фактическим usage и cost, если они известны.
Unknown cost остаётся `NULL`, а не подменяется нулём. Prompts, responses и
source prose не попадают в публичные логи и GitHub comments.

Секреты читаются только из environment и GitHub secrets. Их нельзя записывать в
prompts, fixtures, логи, банк знаний или git history.
