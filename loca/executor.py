"""Исполнитель плана: подзадача → чистый субагент → проверка приёмки → откат при провале.

Цикл ведёт Python, а не модель. Причин три:

* Ollama держит KV-кэш последнего запроса. Если родитель и субагент ходят по
  очереди, префикс каждый раз разный и контекст родителя (8–12k токенов)
  переоценивается после каждой подзадачи — на 4B это десятки секунд впустую.
  Здесь модель вызывается на планирование и на разбор провала, а между ними
  работает код.
* Контекст родителя не растёт: он получает один короткий отчёт, а не выводы
  всех инструментов.
* Выбор готового узла, счёт попыток и откат — арифметика. Модели тут нечего
  решать, а ошибиться она может.

Приёмку запускает исполнитель, а не субагент: «я всё сделал» должно быть
проверено кодом с кодом возврата, иначе это просто слова.
"""
from __future__ import annotations

import os
import subprocess
import time

from .plan import accept_text
from .tools.base import ToolError, truncate
from .tools.codemap import ReadSymbol, RepoMap
from .tools.shell import Bash
from .tools.tests import RunTests
from .ui import dim, green, yellow

# Признаки того, что сломана сама проверка, а не работа субагента: в этом случае
# виноват план, повторять попытку и откатывать правки бессмысленно.
#
# Сюда попадает только то, что не починит никакая работа субагента: нет самой
# программы, нечем запускать тесты, неверные флаги. «Файл не найден» и «модуль не
# найден» — наоборот, обычный признак невыполненной работы (подзадача как раз и
# должна была создать этот файл), поэтому такое считается провалом, а не поломкой.
BROKEN_MARKERS = (
    "command not found", "No test setup", "unrecognized arguments",
    "is not recognized as an internal",
    # сломан не код, а окружение: после brew-обновления питона pytest остаётся
    # у старого интерпретатора, и субагент тут ни при чём
    "no module named pytest", "no module named 'pytest'",
)
MAX_ERROR_TAIL = 700
MAX_REPORT_TAIL = 400


def _tail(text: str, limit: int = MAX_ERROR_TAIL) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def looks_broken(output: str, code: int) -> bool:
    if code in (126, 127):
        return True
    return any(marker.lower() in (output or "").lower() for marker in BROKEN_MARKERS)


def run_command(command: str, ctx, timeout: int) -> tuple[int, str]:
    env = {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat", "TERM": "dumb",
           "CI": "1", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.run(["/bin/bash", "-c", command], cwd=ctx.cwd, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"[проверка прервана: таймаут {timeout}s]"
    except OSError as e:
        return 127, f"[проверка не запустилась: {e}]"
    return proc.returncode, (proc.stdout or "").strip()


def check_acceptance(node: dict, ctx, strict: bool = True) -> tuple[bool, bool, str]:
    """Возвращает (прошла, сломана_проверка, вывод).

    `strict=False` — это предпроверка, до работы субагента: там провал ожидаем и
    «сломанной» проверку делает только отказ в разрешении, иначе подзадача
    «создай файл» блокировалась бы на том, что файла ещё нет.
    """
    accept = node.get("accept") or {"kind": "tests"}
    kind = accept.get("kind", "tests")
    timeout = int(ctx.cfg.get("accept_timeout", 180))

    if kind == "symbol":
        args = {"name": accept["name"]}
        if accept.get("path"):
            args["path"] = accept["path"]
        try:
            out = ReadSymbol().run(args, ctx)
        except ToolError as e:
            message = str(e)
            # «встречается в нескольких файлах» — критерий неоднозначный, это вина плана
            return False, strict and "нескольких" in message, message
        except Exception as e:  # noqa: BLE001
            return False, strict, f"{type(e).__name__}: {e}"
        return True, False, _tail(out, 200)

    if kind == "cmd":
        command, tool, args = accept["command"], Bash(), {"command": accept["command"]}
    else:
        tool = RunTests()
        args = {k: accept[k] for k in ("target", "pattern") if accept.get(k)}
        try:
            command, _framework = tool.build(args, ctx)
        except ToolError as e:
            return False, strict, str(e)

    allowed, feedback = ctx.permissions.check(tool, args, ctx)
    if not allowed:
        return False, True, feedback or "проверка не разрешена пользователем"

    code, output = run_command(command, ctx, timeout)
    if kind == "tests":
        ctx.tests_run += 1
        ctx.tests_passed = code == 0
    if code == 0:
        return True, False, _tail(output, 200)
    broken = strict and looks_broken(output, code)
    return False, broken, f"$ {command}\n{_tail(output)}\n[exit code {code}]"


# ---------------------------------------------------------------- контракт
def file_outlines(node: dict, ctx) -> str:
    """Карта символов только тех файлов, которые правит подзадача."""
    limit = int(ctx.cfg.get("plan_outline_files", 3))
    parts = []
    for rel in node["files"][:limit]:
        try:
            path = ctx.resolve(rel)
        except Exception:  # noqa: BLE001
            continue
        if not path.is_file():
            parts.append(f"{rel}: файла ещё нет — его нужно создать")
            continue
        try:
            parts.append(truncate(RepoMap().run({"path": rel}, ctx), 700))
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(parts)


def contract(node: dict, plan, ctx, attempt: int = 1, last_error: str = "") -> str:
    """Всё, что нужно субагенту, и ничего лишнего: он не видит диалог родителя."""
    lines = [f"Подзадача {node['id']}: {node['title']}", ""]
    if node["files"]:
        lines.append("Менять нужно эти файлы: " + ", ".join(node["files"])
                     + ". Если придётся тронуть другой файл — сделай это и скажи в отчёте.")
    deps = [plan.get(d) for d in node["depends_on"]]
    done_deps = [d for d in deps if d]
    if done_deps:
        lines.append("Уже сделано до тебя:")
        for dep in done_deps:
            extra = f" → результат в {{{{{dep['result_var']}}}}}" if dep.get("result_var") else ""
            lines.append(f"  {dep['id']}: {dep['title']}{extra}")
    outlines = file_outlines(node, ctx)
    if outlines:
        lines += ["", "Карта файлов:", outlines]
    lines += ["", f"Готово, когда: {accept_text(node)}.",
              "Эту проверку я запущу сам после твоего ответа, поэтому не отчитывайся об "
              "успехе наугад — лучше прогони её сам и убедись."]
    if node.get("result_var"):
        lines.append(f"Результат сохрани: set_var(\"{node['result_var']}\", <что получилось>).")
    if attempt > 1 and last_error:
        lines += ["", f"Попытка {attempt}. Прошлая не прошла проверку:", last_error,
                  "Правки прошлой попытки откачены — начинай с исходного состояния файлов "
                  "и устрани саму причину, а не симптом."]
    lines.append("В конце — короткий отчёт: что изменил (path:line) и что не получилось.")
    return "\n".join(lines)


# ---------------------------------------------------------------- исполнитель
class Executor:
    def __init__(self, ctx, ui=None):
        self.ctx = ctx
        self.ui = ui or ctx.ui
        self.plan = ctx.plan
        self.stats = ctx.plan_stats
        self.started = time.time()

    # ------------------------------------------------------------ откат
    def _rollback(self, mark) -> None:
        snaps = self.ctx.snapshots
        if mark is None or snaps is None or not self.ctx.cfg.get("plan_rollback", True):
            return
        try:
            restored = snaps.rollback_to(mark)
        except Exception as e:  # noqa: BLE001
            self.ui.warn(f"откат не удался: {e}")
            return
        if not restored:
            return
        self.stats["rollbacks"] = self.stats.get("rollbacks", 0) + 1
        for path in restored:
            key = str(path)
            if key in self.ctx.read_files:
                if path.is_file():
                    self.ctx.read_files[key] = path.stat().st_mtime
                else:
                    self.ctx.read_files.pop(key, None)
        self.ui.info(f"↩ откат {len(restored)} правок к состоянию до подзадачи")

    # ------------------------------------------------------------ узел
    def _execute(self, node: dict) -> None:
        cfg = self.ctx.cfg
        max_attempts = max(1, int(cfg.get("plan_max_attempts", 2)))
        node["status"] = "running"
        if not self.ui.quiet:
            print(yellow(f"  ▶ {node['id']}  {node['title']}"))

        # Предпроверка: если критерий зелёный ещё до работы, он ничего не проверяет.
        ok, broken, output = self._check(node, strict=False)
        if broken:
            node.update(status="blocked", error="проверку не разрешили запустить: "
                                                + _tail(output, 300))
            return
        if ok:
            node.update(status="blocked",
                        error=("приёмка проходит ещё до работы над подзадачей — такой критерий "
                               "ничего не проверяет, замените его на конкретный"))
            return

        mark = self.ctx.snapshots.mark() if self.ctx.snapshots is not None else None
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            node["attempts"] += 1
            self.stats["subagents"] = self.stats.get("subagents", 0) + 1
            prompt = contract(node, self.plan, self.ctx, attempt, last_error)
            try:
                report = self.ctx.spawn_subagent(f"{node['id']}: {node['title']}"[:60],
                                                 prompt, "code")
            except KeyboardInterrupt:
                node.update(status="failed", error="прервано пользователем")
                raise
            ok, broken, output = self._check(node)
            if ok:
                node.update(status="done", error="", note=_tail(report, MAX_REPORT_TAIL))
                self._save_result(node, report)
                if not self.ui.quiet:
                    print(green(f"  ☑ {node['id']}  приёмка пройдена"))
                return
            if broken:
                node.update(status="blocked", error="проверка не запускается: " + _tail(output, 300))
                self._rollback(mark)
                return
            last_error = _tail(output)
            if not self.ui.quiet:
                print(dim(f"  ✗ {node['id']}  приёмка не прошла (попытка {attempt})"))
            if attempt < max_attempts:
                self._rollback(mark)
        node.update(status="failed", error=last_error)
        self._rollback(mark)

    def _check(self, node: dict, strict: bool = True) -> tuple[bool, bool, str]:
        self.stats["accept_runs"] = self.stats.get("accept_runs", 0) + 1
        return check_acceptance(node, self.ctx, strict=strict)

    def _save_result(self, node: dict, report: str) -> None:
        name = node.get("result_var")
        if not name or name in self.ctx.vars:      # субагент мог заполнить её сам
            return
        try:
            self.ctx.vars.set(name, _tail(report, MAX_REPORT_TAIL))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ цикл
    def run(self) -> str:
        budget = int(self.ctx.cfg.get("max_plan_subagents", 12))
        stopped = ""
        while True:
            ready = self.plan.ready()
            if not ready:
                break
            if self.stats.get("subagents", 0) >= budget:
                stopped = (f"Остановился: израсходован бюджет субагентов ({budget}). "
                           "Оставшиеся подзадачи не запускались.")
                break
            if self.ctx.deadline and time.time() > self.ctx.deadline:
                self.ctx.budget_hit = True
                stopped = ("Остановился: вышло время на задачу. Оставшиеся подзадачи "
                           "не запускались.")
                break
            self._execute(ready[0])
        self.stats["seconds"] = round(self.stats.get("seconds", 0.0) + time.time() - self.started, 1)
        return self._report(stopped)

    def _report(self, stopped: str = "") -> str:
        done, total = self.plan.counts()
        head = (f"Исполнение плана: {done}/{total} подзадач готово · "
                f"субагентов {self.stats.get('subagents', 0)} · "
                f"откатов {self.stats.get('rollbacks', 0)} · "
                f"{self.stats.get('seconds', 0)}s")
        lines = [head]
        for node in self.plan:
            if node["status"] == "done":
                lines.append(f"☑ {node['id']} {node['title']}")
            elif node["status"] == "pending":
                waiting = self.plan.waiting_for(node)
                why = f"ждёт {', '.join(waiting)}" if waiting else "не запускалась"
                lines.append(f"⊘ {node['id']} {node['title']} — {why}")
            else:
                lines.append(f"✗ {node['id']} {node['title']} — {node['status']}: "
                             f"{_tail(node.get('error', ''), 300)}")
        if stopped:
            lines.append(stopped)
        stuck = [n for n in self.plan if n["status"] in ("failed", "blocked")]
        if stuck:
            lines.append(
                "Дальше: разберите причину и пришлите новый plan_write — провалившиеся подзадачи "
                "разбейте мельче или замените проверку; готовые подзадачи с теми же id и title "
                "сохранят статус и переделываться не будут. Правки провалившихся подзадач "
                "откачены, файлы в исходном состоянии.")
        elif self.plan.unfinished():
            lines.append("Дальше: вызовите run_plan ещё раз, чтобы продолжить.")
        else:
            lines.append("Все подзадачи прошли приёмку. Проверьте результат целиком "
                         "(run_tests) и коротко ответьте пользователю.")
        return "\n".join(lines)
