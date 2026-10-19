"""Разбиение задачи (граф подзадач), режим плана и субагенты."""
from __future__ import annotations

from ..plan import MAX_NODES, PlanError, narrow_acceptance, normalize
from ..ui import bold, dim, green
from .base import Tool, ToolError, obj

SUBAGENT_TYPES = ("research", "code")


class PlanWrite(Tool):
    name = "plan_write"
    description = (
        "Split the request into subtasks and record the plan. REQUIRED before anything that "
        "needs 3+ actions or touches 3+ files. Send the whole plan every time: 2-7 subtasks, "
        "each with the files it changes, depends_on (ids that must be done first) and accept — "
        "an EXECUTABLE check that is DIFFERENT for each subtask — a whole-suite run only "
        "passes for the last one, so narrow it: {\"kind\":\"tests\",\"pattern\":\"test_name\"}, "
        "{\"kind\":\"tests\",\"target\":\"tests/test_x.py\"}, "
        "{\"kind\":\"cmd\",\"command\":\"python3 main.py\"} (exit code 0) or "
        "{\"kind\":\"symbol\",\"path\":\"a.py\",\"name\":\"f\"}. A subtask without a check is "
        "rejected; a plain text criterion is not accepted. Then call run_plan to execute it.")
    parameters = obj({
        "tasks": {
            "type": "array",
            "description": f"The complete list of subtasks, 2-{MAX_NODES}, in dependency order",
            "items": {"type": "object", "properties": {
                "id": {"type": "string", "description": "short id, e.g. n1 (assigned by order if omitted)"},
                "title": {"type": "string", "description": "imperative, e.g. 'add --width flag to app/cli.py'"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "files this subtask changes"},
                "depends_on": {"type": "array", "items": {"type": "string"},
                               "description": "ids of subtasks that must be done first"},
                "accept": {"type": "object", "description": "executable check for this subtask",
                           "properties": {
                               "kind": {"type": "string", "enum": ["tests", "cmd", "symbol"]},
                               "command": {"type": "string", "description": "for kind=cmd"},
                               "target": {"type": "string", "description": "for kind=tests: one test file"},
                               "pattern": {"type": "string", "description": "for kind=tests: -k filter"},
                               "path": {"type": "string", "description": "for kind=symbol"},
                               "name": {"type": "string", "description": "for kind=symbol"}}},
                "result_var": {"type": "string",
                               "description": "optional variable name to leave the result in"},
            }, "required": ["title", "accept"]},
        },
    }, ["tasks"])

    def summary(self, args):
        tasks = args.get("tasks") or []
        return f"{len(tasks)} подзадач" if isinstance(tasks, list) else "…"

    def run(self, args, ctx):
        try:
            nodes = normalize(args.get("tasks"))
        except PlanError as e:
            ctx.plan_stats["rejected"] = ctx.plan_stats.get("rejected", 0) + 1
            raise ToolError(f"План не принят — {e}")
        ctx.plan_stats["writes"] = ctx.plan_stats.get("writes", 0) + 1
        narrowed = narrow_acceptance(nodes, lambda node: find_test_for(node, ctx))
        notes = narrowed + ctx.plan.replace(nodes)
        if not ctx.ui.quiet:
            print(ctx.plan.render())
        done, total = ctx.plan.counts()
        head = f"План принят: {total} подзадач, готово {done}."
        if ctx.plan_stats.get("writes", 0) >= 4 and not ctx.plan_stats.get("subagents"):
            head += (" Вы переписываете план уже четвёртый раз, не запустив ни одной подзадачи: "
                     "либо вызовите run_plan, либо сделайте работу сами и проверьте тестами.")
        if total < int(ctx.cfg.get("plan_min_nodes", 3)):
            tail = ("Подзадач мало — run_plan их не возьмёт, сделайте работу сами по этому плану "
                    "и проверьте результат.")
        else:
            tail = "Дальше вызовите run_plan: каждую подзадачу выполнит отдельный субагент."
        return " ".join([head, *[f"Замечание: {n}." for n in notes], tail])


class RunPlan(Tool):
    name = "run_plan"
    description = (
        "Execute the plan made by plan_write. Each ready subtask goes to a fresh coding "
        "subagent with its own context, then ITS ACCEPTANCE CHECK IS RUN FOR REAL; on failure "
        "the edits are rolled back and it is retried, then reported as failed. Returns a short "
        "report. Call it right after plan_write and do not do the subtasks yourself. "
        "Call it again after a new plan_write to continue.")
    parameters = obj({}, [])
    read_only = False
    kind = "exec"

    def summary(self, args):
        return "выполнить план"

    def run(self, args, ctx):
        from ..executor import Executor
        if not len(ctx.plan):
            raise ToolError("Плана нет. Сначала опишите подзадачи в plan_write.")
        if ctx.spawn_subagent is None:
            raise ToolError("субагенты здесь недоступны")
        min_nodes = int(ctx.cfg.get("plan_min_nodes", 3))
        min_files = int(ctx.cfg.get("plan_min_files", 2))
        if len(ctx.plan) < min_nodes or len(ctx.plan.files()) < min_files:
            ctx.plan_stats["refused"] = ctx.plan_stats.get("refused", 0) + 1
            return (f"План слишком маленький для делегирования ({len(ctx.plan)} подзадач, "
                    f"{len(ctx.plan.files())} файлов). Сделайте эту работу сами по плану, "
                    "а в конце прогоните run_tests.")
        if not ctx.plan.ready():
            return _stuck_report(ctx)
        return Executor(ctx).run()


def _stuck_report(ctx) -> str:
    """Ни одна подзадача не готова: объясняем причину и даём ровно два выхода."""
    plan = ctx.plan
    stuck = [n for n in plan if n["status"] in ("failed", "blocked")]
    signature = ";".join(f"{n['id']}:{n['status']}" for n in plan)
    repeated = ctx.plan_stats.get("_stuck_sig") == signature
    ctx.plan_stats["_stuck_sig"] = signature
    lines = ["Запускать нечего: ни одна подзадача не готова."]
    for node in stuck:
        blocked = [n["id"] for n in plan if node["id"] in n["depends_on"]
                   and n["status"] != "done"]
        lines.append(f"✗ {node['id']} {node['title']} — {node['status']}: "
                     + str(node.get("error", ""))[:300])
        if blocked:
            lines.append(f"  из-за неё стоят: {', '.join(blocked)}")
    if not stuck:
        lines.append(plan.summary())
    if repeated:
        lines.append("Повторный вызов run_plan ничего не изменит: состояние плана то же самое.")
    lines.append(
        "Два выхода, третьего нет: (1) пришлите новый plan_write, где провалившаяся подзадача "
        "разбита на более мелкие с другой приёмкой — готовые подзадачи с теми же id и title "
        "сохранятся; (2) доделайте эту часть сами (read_symbol → edit_file → run_tests) и "
        "ответьте пользователю. Правки провалившихся подзадач уже откачены.")
    return "\n".join(lines)


class ExitPlanMode(Tool):
    name = "exit_plan_mode"
    description = ("Only in plan mode. Call it when you have finished exploring and have a plan: "
                   "pass the plan as short numbered steps. The user approves it (then you may edit "
                   "files) or rejects it with feedback. Do not call it just to describe what you read.")
    parameters = obj({
        "plan": {"type": "string", "description": "The plan: numbered steps, including how it will be verified"},
    }, ["plan"])
    read_only = True     # asks the user itself, so no permission check
    kind = "other"

    def summary(self, args):
        return str(args.get("plan", ""))[:60].replace("\n", " ")

    def run(self, args, ctx):
        plan = str(args.get("plan") or "").strip()
        if not plan:
            raise ToolError("plan is empty")
        if ctx.permissions.mode != "plan":
            return ("Not in plan mode — no approval needed. Just do the work: "
                    "put the subtasks in plan_write and call run_plan.")
        try:
            ctx.vars.set("plan", plan)
        except Exception:  # noqa: BLE001  (a full variable store must not block the plan)
            pass
        print()
        print(bold("План:"))
        print("\n".join("  " + l for l in plan.splitlines()))
        if not ctx.permissions.interactive:
            return ("Plan recorded (non-interactive run, saved as {{plan}}). "
                    "Stop here and let the user approve it.")
        try:
            answer = input(f"\n  Принять план? [y] да · [n] нет · "
                           f"{dim('или текст — правки')}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "User interrupted. Stop and wait for instructions."
        low = answer.lower()
        if low in ("y", "yes", "д", "да", ""):
            ctx.permissions.mode = "accept-edits"
            if ctx.refresh_system:
                ctx.refresh_system()
            print(green("  ✓ план принят, режим accept-edits"))
            return ("Plan approved; you may edit files now (mode: accept-edits). "
                    "Turn the plan into subtasks with plan_write and call run_plan. "
                    "The plan text is also saved as {{plan}}.")
        if low in ("n", "no", "н", "нет"):
            return "User rejected the plan. Ask what to change; stay in plan mode."
        return f"User rejected the plan with feedback: {answer}. Revise it and call exit_plan_mode again."


class Task(Tool):
    name = "task"
    description = ("Delegate a self-contained subtask to a subagent with its own fresh context. "
                   "subagent_type='research' gives it read-only tools (read_file, list_dir, glob, "
                   "grep) — use it to explore a large codebase without filling your context. "
                   "subagent_type='code' gives it full tools (edit, bash, run_tests) — use it for "
                   "one-off work that is not part of the plan; for the plan itself use run_plan. "
                   "The prompt must be complete: the subagent cannot see this conversation. It "
                   "shares your variables, so tell it which {{vars}} to read and which set_var "
                   "names to leave its results in. Returns its report.")
    parameters = obj({
        "description": {"type": "string", "description": "3-5 word label"},
        "prompt": {"type": "string", "description": "Complete instructions, and what to report back"},
        "subagent_type": {"type": "string", "enum": list(SUBAGENT_TYPES),
                          "description": "research (read-only, default) or code (can change files)"},
    }, ["description", "prompt"])

    def summary(self, args):
        kind = args.get("subagent_type") or "research"
        return f"{kind}: {str(args.get('description', ''))[:60]}"

    def run(self, args, ctx):
        if ctx.spawn_subagent is None:
            raise ToolError("subagents are not available here")
        kind = (args.get("subagent_type") or "research").strip().lower()
        if kind not in SUBAGENT_TYPES:
            kind = "research"
        if kind == "code" and ctx.permissions.mode == "plan":
            raise ToolError("Plan mode: a code subagent may not change files. "
                            "Use subagent_type='research' or present your plan.")
        if not ctx.ui.quiet:
            print(green(f"  ↳ субагент ({kind}): {args['description']}"))
        return ctx.spawn_subagent(args["description"], args["prompt"], kind)


def find_test_for(node: dict, ctx) -> str | None:
    """Тестовый файл, который проверяет именно эту подзадачу: сам тест среди её files
    или tests/test_<имя модуля>.py рядом с проектом."""
    from pathlib import Path

    from .fs import IGNORED_DIRS
    files = [str(f) for f in node.get("files") or []]
    for rel in files:
        p = Path(rel)
        if p.suffix == ".py" and (p.name.startswith("test_") or p.stem.endswith("_test")
                                  or "tests" in p.parts):
            return rel
    stems = [Path(rel).stem for rel in files if Path(rel).suffix == ".py"]
    stems = [s for s in stems if s not in ("__init__", "__main__", "main", "setup", "conftest")]
    if not stems:
        return None
    root = Path(ctx.cwd)
    for stem in stems:
        for pattern in (f"test_{stem}.py", f"{stem}_test.py"):
            for found in sorted(root.rglob(pattern)):
                rel = found.relative_to(root)
                if any(part in IGNORED_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                    continue
                return str(rel)
    return None
