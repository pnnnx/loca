# Правила работы в репозитории loca

- Перед работой: прочитать `HANDOFF.md` (состояние) и верх `docs/JOURNAL.md`. План всего проекта —
  `~/Developer/coder/PLAN.md`, узкие места — `~/Developer/coder/docs/BOTTLENECKS.md`.
- Только stdlib, без зависимостей. Любая правка поведения — с тестом в `tests/`; `python3 -m pytest -q` зелёный.
- После каждого действия: запись сверху `docs/JOURNAL.md` (Что / Как / Зачем / Проверка / Коммит),
  при изменении состояния — `HANDOFF.md`, коммит в markdown по шаблону `.gitmessage`
  (разделы `## Что`, `## Как`, `## Зачем`, `## Проверка`). Хуки `.githooks/` это проверяют.
- Области в заголовке коммита: `agent`, `plan`, `tools`, `llm`, `cli`, `bench`, `vim`, `docs`.
- После clone: `git config core.hooksPath .githooks && git config commit.template .gitmessage`.
