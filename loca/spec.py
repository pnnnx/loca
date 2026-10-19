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
- Если просьба размытая («сделай лучше», «добавь X» без подробностей) — НЕ придумывай цель: задай
  вопросы, от ответа на которые зависит, что именно делать.
- Пути бери только из карты проекта. «(новый)» — только для файла, которого в карте нет.
- Критерий готовности должен проверяться запуском (команда или тест), а не словами «работает правильно».

Пример размытой просьбы «добавь логирование» в проекте с app/server.py:
Цель: добавить логирование в app/server.py (что и куда логировать — уточнить)
Файлы: app/server.py
Ограничения: нет
Готово, когда: `python -m pytest -q` проходит и при запуске сервера в лог пишутся строки
Предположения: модуль logging из стандартной библиотеки
Вопросы: 1) Что логировать: запросы, ошибки, всё? 2) Куда: в консоль или в файл?

Пример ясной просьбы «в app/util.py функция slug(s) должна убирать пробелы по краям»:
Цель: slug(s) в app/util.py обрезает пробелы по краям строки
Файлы: app/util.py
Ограничения: остальное поведение slug не менять
Готово, когда: `python -c "from app.util import slug; assert slug(' a ') == slug('a')"` проходит
Предположения: нет
Вопросы: нет"""

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
    return check_spec(clean_spec(resp.content), request, ctx)


FIELDS = ("Цель", "Файлы", "Ограничения", "Готово, когда", "Предположения", "Вопросы")
_PATH_RE = re.compile(r"(?<![\w/])/?(?:[\w.-]+/)*[\w-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|c|h|cpp|"
                      r"json|toml|ya?ml|md|txt|cfg|ini|sh)\b")
_ANCHOR_RE = re.compile(r"[\w-]+\.\w{1,4}\b|`|\w+\(|\b[a-z]+_[a-z_]+\b|\b[a-z]+[A-Z]\w*|\d")
_NONE_RE = re.compile(r"^[\s«»\"'.*-]*(нет|none|—|-)?[\s«»\"'.*]*$", re.I)


def is_vague(request: str) -> bool:
    """Короткая просьба без единой зацепки: ни файла, ни имени из кода, ни числа."""
    text = (request or "").strip()
    return len(text.split()) <= 10 and not _ANCHOR_RE.search(text)


def _field_line(text: str, name: str) -> re.Match | None:
    names = "|".join(map(re.escape, FIELDS))
    return re.search(rf"^(\W*{re.escape(name)}\W*:)(.*?)(?=^\W*(?:{names})\W*:|\Z)", text, re.M | re.S)


def check_spec(spec: str, request: str, ctx) -> str:
    """Поправки, которые не зависят от модели (маленькая модель их стабильно путает).

    - «(новый)» у файла, который есть в проекте, — снимаем; файл, которого нет и не помечен
      «(новый)», — помечаем «(нет в проекте)»: человек увидит выдуманный путь до работы.
    - размытая просьба без вопросов и предположений — модель додумала цель сама: добавляем вопрос.
    """
    m = _field_line(spec, "Файлы")
    if m:
        body = m.group(2)

        def fix(pm: re.Match) -> str:
            rel = pm.group(0)
            try:
                exists = ctx.resolve(rel).exists()
            except Exception:  # noqa: BLE001
                return rel
            tail = body[pm.end():pm.end() + 12].lower()
            if exists:
                return ctx.rel(ctx.resolve(rel))
            return rel if "нов" in tail else rel + " (нет в проекте)"

        new_body = _PATH_RE.sub(fix, body)
        for pm in _PATH_RE.finditer(new_body):
            try:
                if ctx.resolve(pm.group(0)).exists():
                    new_body = re.sub(rf"({re.escape(pm.group(0))}\W{{0,3}})\(новый\)", r"\1", new_body)
            except Exception:  # noqa: BLE001
                pass
        new_body = re.sub(r"[ \t]{2,}", " ", new_body)
        spec = spec[:m.start(2)] + new_body + spec[m.end(2):]
    q, a = _field_line(spec, "Вопросы"), _field_line(spec, "Предположения")
    no_q = not q or _NONE_RE.match(q.group(2).strip())
    no_a = not a or _NONE_RE.match(a.group(2).strip())
    if is_vague(request) and no_q and no_a:
        ask = ("1) Просьба без подробностей — что именно должно измениться и как это проверить "
               "(пример входа и ожидаемого результата)?")
        if q:
            spec = spec[:q.start(2)] + " " + ask + "\n" + spec[q.end(2):]
        else:
            spec = spec.rstrip() + f"\nВопросы: {ask}"
    return spec.strip()


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
