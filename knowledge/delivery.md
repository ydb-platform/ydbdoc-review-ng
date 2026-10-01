# Конвейер разработки

1. `git pull --ff-only` от `public/main` перед общей правкой.
2. Буквально по `REQUIREMENTS_RU.md`. Новые решения — только после вопроса
   пользователю.
3. Только `main`: частые commits и push. Без feature branches для этой работы.
4. Два потока — только без общих production-файлов; commits сериализовать.
5. После каждой атомарной задачи: focused tests + независимый review diff.
6. После всех задач: полный suite → tag → удалить старую translation branch
   тестового PR → новый `doc_translate` → цель честный GREEN/YELLOW арбитра.
7. Каждое уточнение контракта сразу писать в `knowledge/` и коммитить в `main`.
