"""Задание перед работой: «понял так — выполнять?».

Главная беда маленькой модели — не код, а понимание запроса: она достраивает размытую
просьбу своими догадками и уверенно делает не то. Поэтому перед задачей, которая
меняет файлы, модель отдельным коротким запросом (без инструментов) переписывает
просьбу в явное задание: цель, файлы, ограничения, проверяемый критерий готовности,
предположения и вопросы. Человек подтверждает или поправляет одной фразой — ошибка
понимания ловится до того, как агент потратит десять минут и испортит файлы.

Задание опирается на карту проекта (repo_map), иначе модель выдумывает пути.
"""
from __future__ import annotations

import re

from .agent import looks_multifile
from .tools.base import truncate

# глаголы, после которых агент полезет менять файлы
ACTION_RE = re.compile(
    r"\b(добав|сдела|исправ|почин|реализ|напиш|перепиш|переимен|удал|вынес|перенес|"
    r"отрефактор|рефактор|замени|обнов|созда|допиш|внедр|подключ|поправ|измени|убери|почисти|"
    r"раздели|оптимизир|ускор|передела|додела|настро|сгенерир|"
    r"add|fix|implement|create|write|rewrite|rename|remove|delete|refactor|extract|move|"
    r"update|replace|build|make)\w*", re.I)
# вопросы «почему / что выведет / объясни» ничего не меняют — подтверждать нечего
QUESTION_RE = re.compile(
    r"^\s*(почему|зачем|что|как|какой|какая|какие|объясни|расскажи|покажи|где|когда|"
    r"why|what|how|explain|show|where|when)\b", re.I)

SPEC_SYSTEM = """\
Ты помощник, который переводит просьбу пользователя в точное задание для программиста.
Ничего не выполняй и не пиши код. Ответь ровно в этом формате, по-русски, коротко:

Цель: <одно предложение — что должно получиться>
Файлы: <пути из карты проекта, которые придётся менять; новые файлы пометь «(новый)»>
Ограничения: <что нельзя ломать или менять; «нет», если в просьбе ничего такого нет>
Готово, когда: <проверка, которую можно ЗАПУСТИТЬ: команда, тест или «функция X существует»>
Предположения: <всё, чего НЕТ в просьбе, но ты это допускаешь; «нет», если допущений нет>
Вопросы: <0–3 вопроса, только если от ответа зависит результат; иначе «нет»>

Правила:
- Не добавляй требований от себя. Всё, чего пользователь не сказал, — это предположение, а не цель.
- Пути бери только из карты проекта. Если нужного файла нет — так и напиши.
- Критерий готовности должен проверяться запуском, а не словами «работает правильно»."""

CONFIRM_HINT = "Enter — выполнять · текст — поправить/ответить на вопросы · n — отмена"
SPEC_MARK = "[Задание, подтверждённое пользователем]"


def needs_spec(text: str, cfg: dict) -> bool:
    mode = str(cfg.get("spec", "auto")).lower()
    if mode in ("off", "false", "0", "no"):
        return False
    if mode in ("always", "on", "true", "1"):
        return True
    text = (text or "").strip()
    if not text or SPEC_MARK in text:
        return False
    if looks_multifile(text):
        return True
    if QUESTION_RE.match(text):
        # «почему падает после рефакторинга?» — вопрос; «как это сделать? сделай» — уже просьба
        parts = re.split(r"[?.!]\s+", text, maxsplit=1)
        return len(parts) > 1 and bool(ACTION_RE.search(parts[1]))
    return bool(ACTION_RE.search(text))


def project_map(ctx, limit: int = 2500) -> str:
    from .tools.codemap import RepoMap
    try:
        text = RepoMap().run({"max_files": 40}, ctx)
        if len(text) > limit:      # для задания важнее полный список файлов, чем сигнатуры
            text = RepoMap().run({"max_files": 40, "detail": "names"}, ctx)
        return truncate(text, limit)
    except Exception:  # noqa: BLE001 — пустой проект или нет кода: задание всё равно нужно
        return "(файлов с кодом нет)"


def make_spec(llm, ctx, request: str, corrections: list[str] | None = None) -> str:
    user = [f"Карта проекта:\n{project_map(ctx)}", f"Просьба пользователя:\n{request.strip()}"]
    for i, c in enumerate(corrections or [], 1):
        user.append(f"Уточнение пользователя {i}:\n{c.strip()}")
    user.append("Составь задание строго в заданном формате.")
    resp = llm.chat([{"role": "system", "content": SPEC_SYSTEM},
                     {"role": "user", "content": "\n\n".join(user)}], tools=None, think=False)
    return clean_spec(resp.content)


def clean_spec(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```\w*\n|```$", "", text).strip()
    return text


def with_spec(request: str, spec: str, corrections: list[str] | None = None) -> str:
    """Сообщение агенту: исходная просьба + подтверждённое задание (+ ответы пользователя)."""
    parts = [request.strip(), "", SPEC_MARK, spec.strip()]
    if corrections:
        parts += ["", "Ответы и уточнения пользователя:"] + [f"- {c.strip()}" for c in corrections]
    parts += ["", "Работай по этому заданию. Предположения из него не превращай в лишнюю работу; "
                  "в конце проверь критерий «Готово, когда» запуском."]
    return "\n".join(parts)


def confirm_loop(llm, ctx, request: str, ask, show, max_rounds: int = 3) -> str | None:
    """Показывает задание и ждёт решения. Возвращает текст для агента или None (отмена).

    ask(prompt) -> строка ввода, show(text) — печать; вынесены, чтобы тестировать без терминала.
    """
    corrections: list[str] = []
    spec = make_spec(llm, ctx, request)
    for _ in range(max_rounds):
        show(spec)
        answer = ask(CONFIRM_HINT).strip()
        if not answer:
            return with_spec(request, spec, corrections)
        if answer.lower() in ("n", "н", "нет", "no", "отмена", "cancel"):
            return None
        corrections.append(answer)
        spec = make_spec(llm, ctx, request, corrections)
    show(spec)
    return with_spec(request, spec, corrections)
