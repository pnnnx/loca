"""Граф подзадач: план, который исполняется, а не пересказывается.

Плоский todo-список держала в голове сама модель: она решала, что делать
дальше, и на 4B это разваливалось на задачах в три файла. Здесь план —
объект: у подзадачи есть зависимости, список файлов и **исполняемая**
проверка приёмки. Кто готов к запуску, считает код (`ready`), а не модель;
проверку запускает код, поэтому «я всё сделал» нельзя объявить словами.

Формат подзадачи:

    {"id": "n2", "title": "переписать store.py на validate_user",
     "files": ["store.py"], "depends_on": ["n1"],
     "accept": {"kind": "tests", "target": "tests/test_store.py"},
     "result_var": "store_changes"}

Служебные поля (status / attempts / error / note) ставит исполнитель,
модель их не присылает.
"""
from __future__ import annotations

import json
import re

STATUSES = ("pending", "running", "done", "failed", "blocked")
ICONS = {"pending": "☐", "running": "◐", "done": "☑", "failed": "✗", "blocked": "⊘"}
ACCEPT_KINDS = ("tests", "cmd", "symbol")
MAX_NODES = 8
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,16}$")
VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,47}$")


# заголовки-«проверки»: их модель любит выдавать вместо работы
CHECK_TITLE_RE = re.compile(
    r"^\s*(проверить|проверка|запустить|прогнать|убедиться|verify|check|run)\b", re.I)


class PlanError(ValueError):
    """Плохой план. Текст ошибки уходит модели — он должен говорить, как починить."""


# ---------------------------------------------------------------- разбор
def _as_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                return _as_list(json.loads(text))
            except json.JSONDecodeError:
                pass
        return [p.strip() for p in re.split(r"[,\s]+", text) if p.strip()]
    if isinstance(value, (list, tuple)):
        return [v for v in value]
    return [value]


def normalize_accept(raw, where: str) -> dict:
    """Приёмка обязана быть исполняемой: команда, тесты или наличие символа."""
    if raw is None or raw == "":
        raise PlanError(
            f"{where}: нет accept. У каждой подзадачи должна быть проверка, которую можно "
            "запустить: {\"kind\": \"tests\", \"target\": \"tests/test_x.py\"} — прогнать тесты, "
            "{\"kind\": \"cmd\", \"command\": \"python3 main.py\"} — команда с кодом возврата 0, "
            "{\"kind\": \"symbol\", \"path\": \"a.py\", \"name\": \"f\"} — в файле появилось "
            "определение. Текстовый критерий не принимается: его некому проверить.")
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = {"kind": "cmd", "command": text}
        elif text.lower() in ("tests", "test", "run_tests"):
            raw = {"kind": "tests"}
        else:
            raw = {"kind": "cmd", "command": text}
    if not isinstance(raw, dict):
        raise PlanError(f"{where}: accept должен быть объектом, а не {type(raw).__name__}")

    accept = {k: v for k, v in raw.items() if v not in (None, "")}
    kind = str(accept.get("kind") or "").strip().lower()
    if not kind:                      # модель забыла kind — выводим из полей
        if accept.get("command"):
            kind = "cmd"
        elif accept.get("name"):
            kind = "symbol"
        else:
            kind = "tests"
    if kind in ("test", "run_tests", "pytest"):
        kind = "tests"
    if kind in ("command", "shell", "bash"):
        kind = "cmd"
    if kind not in ACCEPT_KINDS:
        raise PlanError(f"{where}: accept.kind = {kind!r}, допустимы {', '.join(ACCEPT_KINDS)}")
    out: dict = {"kind": kind}
    if kind == "cmd":
        command = str(accept.get("command") or accept.get("cmd") or "").strip()
        if not command:
            raise PlanError(f"{where}: для accept.kind='cmd' нужна command")
        out["command"] = command
    elif kind == "tests":
        for key in ("target", "pattern"):
            if accept.get(key):
                out[key] = str(accept[key]).strip()
    else:
        name = str(accept.get("name") or "").strip()
        path = str(accept.get("path") or "").strip()
        if not name:
            raise PlanError(f"{where}: для accept.kind='symbol' нужно name (имя функции или класса)")
        out["name"] = name
        if path:
            out["path"] = path
    return out


def accept_text(node: dict) -> str:
    """Человеческое описание проверки — идёт в контракт подзадачи."""
    accept = node.get("accept") or {}
    kind = accept.get("kind")
    if kind == "cmd":
        return f"команда `{accept['command']}` завершается кодом 0"
    if kind == "symbol":
        where = f" в {accept['path']}" if accept.get("path") else ""
        return f"определение `{accept['name']}`{where} существует"
    bits = []
    if accept.get("target"):
        bits.append(accept["target"])
    if accept.get("pattern"):
        bits.append(f"-k {accept['pattern']}")
    return "тесты проходят" + (f" ({' '.join(bits)})" if bits else "")


def normalize(raw_nodes) -> list[dict]:
    """Приводит присланный моделью план к списку узлов или падает с PlanError."""
    if isinstance(raw_nodes, str):
        try:
            raw_nodes = json.loads(raw_nodes)
        except json.JSONDecodeError:
            raise PlanError("tasks должен быть массивом объектов, а не строкой") from None
    if isinstance(raw_nodes, dict):
        raw_nodes = [raw_nodes]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise PlanError("tasks пуст: пришлите список подзадач целиком")
    if len(raw_nodes) > MAX_NODES:
        raise PlanError(f"подзадач {len(raw_nodes)}, максимум {MAX_NODES}. Укрупните шаги: "
                        "план — это куски работы, а не отдельные вызовы инструментов.")

    nodes: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_nodes, 1):
        where = f"подзадача {i}"
        if isinstance(raw, str):
            raise PlanError(f"{where}: нужен объект с title и accept, а не строка {raw[:40]!r}")
        if not isinstance(raw, dict):
            raise PlanError(f"{where}: нужен объект с title и accept")
        title = str(raw.get("title") or raw.get("content") or "").strip()
        if not title:
            raise PlanError(f"{where}: пустой title")
        node_id = str(raw.get("id") or "").strip() or f"n{i}"
        if not ID_RE.match(node_id):
            raise PlanError(f"{where}: id {node_id!r} — нужен короткий идентификатор, например n{i}")
        if node_id in seen:
            raise PlanError(f"{where}: id {node_id!r} уже занят — идентификаторы должны быть разными")
        seen.add(node_id)
        result_var = str(raw.get("result_var") or "").strip()
        if result_var and not VAR_RE.match(result_var):
            raise PlanError(f"{where}: result_var {result_var!r} — нужен идентификатор, например n{i}_out")
        nodes.append({
            "id": node_id,
            "title": title,
            "files": [str(f).strip() for f in _as_list(raw.get("files")) if str(f).strip()],
            "depends_on": [str(d).strip() for d in _as_list(raw.get("depends_on")) if str(d).strip()],
            "accept": normalize_accept(raw.get("accept"), where),
            "result_var": result_var,
            "status": "pending",
            "attempts": 0,
            "error": "",
            "note": "",
        })

    known = {n["id"] for n in nodes}
    for node in nodes:
        for dep in node["depends_on"]:
            if dep == node["id"]:
                raise PlanError(f"{node['id']} зависит от самой себя")
            if dep not in known:
                raise PlanError(f"{node['id']}: depends_on ссылается на {dep!r}, а такой подзадачи "
                                f"нет. Есть: {', '.join(sorted(known))}")
    cycle = find_cycle(nodes)
    if cycle:
        raise PlanError("зависимости зациклены: " + " → ".join(cycle)
                        + ". План должен быть графом без циклов.")
    checks_only = [n["id"] for n in nodes if not n["files"] and CHECK_TITLE_RE.search(n["title"])]
    # одну «проверочную» подзадачу стерпим (её заблокирует предпроверка с понятным текстом),
    # две и больше — это уже не план работы
    if len(checks_only) >= 2:
        raise PlanError(
            f"подзадачи {', '.join(checks_only)} — это проверки, а не работа "
            "(«проверить», «запустить тесты»), и у них нет files. Проверка живёт в accept "
            "каждой подзадачи; в плане должны быть изменения кода с указанными files.")
    return nodes


def find_cycle(nodes: list[dict]) -> list[str]:
    """Возвращает узлы, оставшиеся после топологической сортировки (пусто — циклов нет)."""
    pending = {n["id"]: set(n["depends_on"]) for n in nodes}
    done: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node_id, deps in list(pending.items()):
            if deps <= done:
                done.add(node_id)
                pending.pop(node_id)
                changed = True
    return sorted(pending)


# ---------------------------------------------------------------- план
class Plan:
    def __init__(self, nodes: list[dict] | None = None):
        self.nodes: list[dict] = list(nodes or [])

    def __len__(self) -> int:
        return len(self.nodes)

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def __iter__(self):
        return iter(self.nodes)

    # ------------------------------------------------------------ состав
    def get(self, node_id: str) -> dict | None:
        return next((n for n in self.nodes if n["id"] == node_id), None)

    def replace(self, nodes: list[dict]) -> list[str]:
        """Ставит новый план, сохраняя статус уже сделанных подзадач.

        Перепланирование — это повторный plan_write целиком (патчи слишком легко
        сломать маленькой модели), поэтому готовую работу переносим сюда сами.
        """
        done = {(n["id"], n["title"]): n for n in self.nodes if n["status"] == "done"}
        kept = []
        for node in nodes:
            old = done.get((node["id"], node["title"]))
            if old:
                node.update({"status": "done", "attempts": old.get("attempts", 0),
                             "note": old.get("note", ""), "error": ""})
                kept.append(node["id"])
        self.nodes = list(nodes)
        notes = []
        if kept:
            notes.append(f"уже сделано раньше и переделываться не будет: {', '.join(kept)}")
        missing = [n["id"] for n in self.nodes if not n["files"]]
        if missing:
            notes.append(f"без files: {', '.join(missing)} — укажите, какие файлы меняет подзадача")
        if len(self.nodes) > 2 and not any(n["depends_on"] for n in self.nodes):
            notes.append("ни у одной подзадачи нет depends_on — проверьте, правда ли они независимы")
        # Ловушка, стоившая четырёх откатов в прогоне hard3: у всех подзадач приёмка «весь
        # набор тестов». Тесты описывают конечное состояние, поэтому зелёной станет только
        # последняя, а остальные откатятся как провалившиеся.
        whole = [n["id"] for n in self.nodes if n["accept"].get("kind") == "tests"
                 and not n["accept"].get("target") and not n["accept"].get("pattern")]
        if len(whole) >= 2:
            notes.append(
                f"у {', '.join(whole)} приёмка — весь набор тестов. Пройдёт только последняя: "
                "остальные сделают свою часть, набор всё ещё будет красным, и их правки "
                'откатятся. Сузьте: {"kind": "tests", "pattern": "test_имя"} или '
                '{"kind": "symbol", "path": "a.py", "name": "f"}, а весь набор оставьте последней')
        return notes

    def clear(self) -> None:
        self.nodes = []

    # ------------------------------------------------------------ порядок
    def ready(self) -> list[dict]:
        """Подзадачи, которые можно запускать: сами не начаты, зависимости готовы."""
        done = {n["id"] for n in self.nodes if n["status"] == "done"}
        return [n for n in self.nodes
                if n["status"] == "pending" and set(n["depends_on"]) <= done]

    def waiting_for(self, node: dict) -> list[str]:
        done = {n["id"] for n in self.nodes if n["status"] == "done"}
        return [d for d in node["depends_on"] if d not in done]

    def unfinished(self) -> list[dict]:
        return [n for n in self.nodes if n["status"] != "done"]

    def counts(self) -> tuple[int, int]:
        return sum(1 for n in self.nodes if n["status"] == "done"), len(self.nodes)

    def files(self) -> set[str]:
        return {f for n in self.nodes for f in n["files"]}

    # ------------------------------------------------------------ вид
    def render(self) -> str:
        if not self.nodes:
            return "(плана нет)"
        done, total = self.counts()
        out = [f"  план: {done}/{total}"]
        for node in self.nodes:
            icon = ICONS.get(node["status"], "☐")
            line = f"  {icon} {node['id']}  {node['title']}"
            if node["depends_on"]:
                line += f"  ← {', '.join(node['depends_on'])}"
            out.append(line)
            detail = node.get("error") or node.get("note")
            if detail and node["status"] in ("failed", "blocked"):
                out.append(f"       {detail.strip().splitlines()[0][:100]}")
        return "\n".join(out)

    def summary(self) -> str:
        """Компактный вид для модели: переинжект плана и сжатие контекста."""
        lines = []
        for node in self.nodes:
            line = f"- [{node['status']}] {node['id']} {node['title']}"
            waiting = self.waiting_for(node) if node["status"] == "pending" else []
            if waiting:
                line += f" (ждёт {', '.join(waiting)})"
            if node["status"] in ("failed", "blocked") and node.get("error"):
                line += f" — {node['error'].strip().splitlines()[0][:120]}"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------ сериализация
    def to_list(self) -> list[dict]:
        return [dict(n) for n in self.nodes]

    def load(self, data) -> None:
        """Восстановление из сессии: без валидации, но с защитой от мусора."""
        nodes = []
        for raw in data or []:
            if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
                continue
            node = {"id": str(raw["id"]), "title": str(raw["title"]),
                    "files": [str(f) for f in raw.get("files") or []],
                    "depends_on": [str(d) for d in raw.get("depends_on") or []],
                    "accept": raw.get("accept") if isinstance(raw.get("accept"), dict) else {"kind": "tests"},
                    "result_var": str(raw.get("result_var") or ""),
                    "status": raw.get("status") if raw.get("status") in STATUSES else "pending",
                    "attempts": int(raw.get("attempts") or 0),
                    "error": str(raw.get("error") or ""), "note": str(raw.get("note") or "")}
            if node["status"] == "running":       # процесс прервали — узел не доделан
                node["status"] = "pending"
            nodes.append(node)
        self.nodes = nodes
