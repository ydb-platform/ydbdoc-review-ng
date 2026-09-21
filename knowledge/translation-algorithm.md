# Проверенный алгоритм перевода

## Поля и protected fragments

Parser выделяет цельные переводимые поля и точные protected source fragments.
Ответ модели является JSON map `field_id → string`. Локально проверяются exact
requested IDs, строковые значения, отсутствие duplicate/extra keys, точное
множество placeholders и корректные container pairs.

После проверки placeholders заменяются исходными bytes, candidate собирается и
повторно разбирается как Markdown/YFM. Невалидный ответ не попадает в сборщик.
При `doc_verify` те же exact source fragments сверяются с текущим target:
ручное изменение URL, path или code относительно authoritative source
отвергается без отдельного navigation graph или link resolver.

## Fenced comments

В fenced code используется минимальный quote-aware lexical scanner:

- C++, Java и JavaScript: `//`, `/* ... */`;
- Python, Bash и YAML: `#`;
- HTML: `<!-- ... -->`.

Scanner различает маркеры и те же символы внутри строковых литералов, но не
реализует полную грамматику языка. Неизвестный язык fence целиком protected.

## Проверка качества

После первой валидной публикации critic сравнивает authoritative source и final
target целиком или крупными осмысленными блоками. Он проверяет точность,
полноту, термины и ссылки. Для исправимого finding разрешена одна repair
attempt, затем обязательны повторная сборка, локальные validators, parse и final
critic. Следующих repair attempts нет.

Технический transport retry может быть bounded, но не превращается в
сохраняемую state machine или механизм продолжения.
