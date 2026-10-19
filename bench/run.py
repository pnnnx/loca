#!/usr/bin/env python3
"""Мини-бенчмарк loca: гоняет фиксированные задачи и считает метрики.

    python3 bench/run.py                        # все задачи текущей моделью
    python3 bench/run.py -m qwen3.5:9b --ctx 8192
    python3 bench/run.py --tasks fix_typo,write_test --keep
    python3 bench/run.py --verify-tasks         # проверить сами задачи, без модели
    python3 bench/run.py --out runs/9b.json --baseline runs/4b.json

Без бенчмарка любое изменение промпта, набора инструментов или модели — гадание:
на глаз «стало лучше» неотличимо от везения. Каждая задача выполняется в своей
временной копии проекта; агент запускается с --yolo, то есть выполняет команды
модели без подтверждения — поэтому только в temp-каталоге.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasks import GROUPS, TASKS, by_name  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- подготовка
def materialize(task: dict, root: Path) -> None:
    for rel, content in task["files"].items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if task.get("config"):
        (root / ".loca").mkdir(exist_ok=True)
        (root / ".loca" / "config.json").write_text(
            json.dumps(task["config"], ensure_ascii=False), encoding="utf-8")


AGENT_DIRS = (".loca", ".loca_home", "__pycache__", ".pytest_cache")


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part in AGENT_DIRS for part in path.parts):
            out[str(path.relative_to(root))] = hashlib.sha1(path.read_bytes()).hexdigest()
    return out


def write_verify(task: dict, root: Path) -> None:
    for rel, content in (task.get("verify_files") or {}).items():
        (root / rel).write_text(content, encoding="utf-8")


def run_checks(task: dict, root: Path) -> tuple[bool, str]:
    for cmd in task.get("checks", []):
        proc = subprocess.run(["/bin/bash", "-c", cmd], cwd=root, capture_output=True,
                              text=True, timeout=180)
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
            return False, f"check «{cmd}» → exit {proc.returncode}: {' / '.join(tail)}"
    return True, ""


def score_partial(task: dict, root: Path) -> tuple[int, int, list[str]]:
    """Сколько частичных целей достигнуто. Даёт градиент там, где «не прошло»
    неинформативно: многофайловую задачу слабая модель редко доводит до конца."""
    goals = task.get("partial") or []
    done, failed = 0, []
    for label, cmd in goals:
        try:
            proc = subprocess.run(["/bin/bash", "-c", cmd], cwd=root, capture_output=True,
                                  text=True, timeout=180)
            ok = proc.returncode == 0
        except subprocess.SubprocessError:
            ok = False
        if ok:
            done += 1
        else:
            failed.append(label)
    return done, len(goals), failed


# ---------------------------------------------------------------- прогон
def agent_command(args, workspace: Path, stats_path: Path, prompt: str,
                  limit: int = 0) -> list[str]:
    base = args.loca.split() if args.loca else [sys.executable, "-m", "loca"]
    cmd = base + ["-p", "--yolo", "--cwd", str(workspace), "--stats", str(stats_path)]
    if limit > 0:
        cmd += ["--max-seconds", str(limit)]
    if args.model:
        cmd += ["-m", args.model]
    if args.ctx:
        cmd += ["--ctx", str(args.ctx)]
    if args.host:
        cmd += ["--host", args.host]
    if getattr(args, "backend", None):
        cmd += ["--backend", args.backend]
    if args.think:
        cmd += ["--think"]
    return cmd + [prompt]


def run_task(task: dict, args) -> dict:
    workspace = Path(tempfile.mkdtemp(prefix=f"loca-bench-{task['name']}-"))
    loca_home = Path(tempfile.mkdtemp(prefix=f"loca-home-{task['name']}-"))
    result = {"name": task["name"], "passed": False, "reason": "", "seconds": 0.0}
    try:
        materialize(task, workspace)
        before = snapshot(workspace)
        stats_path = workspace / ".loca_stats.json"
        env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               "LOCA_HOME": str(loca_home)}
        scale = float(getattr(args, "time_scale", 1.0) or 1.0)
        hard = int(task.get("timeout", args.timeout) * scale)
        margin = int(getattr(args, "margin", 45) or 0)
        soft = max(60, hard - margin) if margin > 0 else 0
        started = time.time()
        try:
            proc = subprocess.run(agent_command(args, workspace, stats_path, task["prompt"], soft),
                                  cwd=workspace, env=env, capture_output=True, text=True,
                                  timeout=hard)
            stdout, timed_out = proc.stdout + proc.stderr, False
        except subprocess.TimeoutExpired as e:
            stdout = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
            timed_out = True
        result["seconds"] = round(time.time() - started, 1)

        if stats_path.is_file():
            try:
                raw = json.loads(stats_path.read_text())
                if isinstance(raw.get("plan"), list):
                    result["plan_nodes"] = [{"id": n.get("id"), "status": n.get("status"),
                                             "title": str(n.get("title", ""))[:50]}
                                            for n in raw["plan"]]
                result.update({k: v for k, v in raw.items()
                               if k in ("requests", "steps", "tokens_in", "tokens_out",
                                        "model_seconds", "tools", "tests_run", "tests_passed",
                                        "plan_stats", "touched", "budget_hit",
                                        "tool_errors", "trace")})
            except (OSError, json.JSONDecodeError):
                pass
            stats_path.unlink()

        write_verify(task, workspace)
        if timed_out:
            result["reason"] = f"таймаут {hard}s"
            ok, _ = run_checks(task, workspace)      # вдруг успел доделать до убийства
            result["passed"] = ok and not task.get("expect_output")
        else:
            if result.get("budget_hit"):
                result["reason"] = f"остановлен по --max-seconds {soft}s"
            ok, reason = run_checks(task, workspace)
            if ok and task.get("expect_output"):
                if not re.search(task["expect_output"], stdout):
                    ok, reason = False, f"в ответе нет /{task['expect_output']}/"
            if ok and task.get("readonly"):
                changed = [p for p, h in snapshot(workspace).items()
                           if p not in ("_verify.py",) and before.get(p) != h]
                if changed:
                    ok, reason = False, f"файлы изменены, хотя не требовалось: {', '.join(changed[:3])}"
            result["passed"] = ok
            if not ok or not result.get("budget_hit"):
                result["reason"] = reason if not ok else result.get("reason", "")
        # частичные цели считаем всегда — в том числе после таймаута
        if task.get("partial"):
            done, total, failed = score_partial(task, workspace)
            result.update({"partial": done, "partial_total": total, "partial_failed": failed})
        ok = result["passed"]
        if args.verbose or (not ok and args.show_output):
            print(f"\n----- вывод агента ({task['name']}) -----\n{stdout.strip()[-3000:]}\n")
        return result
    finally:
        if args.keep:
            print(f"  каталог оставлен: {workspace} (состояние агента: {loca_home})")
        else:
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(loca_home, ignore_errors=True)


# ---------------------------------------------------------------- отчёт
def diagnose(result: dict) -> list[str]:
    """Короткий разбор упавшей задачи: где агент буксовал."""
    out = []
    tools = result.get("tools") or {}
    calls = sum(tools.values())
    errors = result.get("tool_errors") or 0
    if calls:
        top = ", ".join(f"{n}×{c}" for n, c in
                        sorted(tools.items(), key=lambda kv: -kv[1])[:4])
        out.append(f"вызовы: {top}" + (f" · ошибок {errors}/{calls}" if errors else ""))
    if errors and calls and errors / calls >= 0.4:
        out.append("больше трети вызовов — ошибки: агент буксовал, а не работал")
    ps = result.get("plan_stats") or {}
    if ps.get("subagents") or ps.get("writes"):
        out.append(f"план: планов {ps.get('writes', 0)}"
                   + (f" (отклонено {ps['rejected']})" if ps.get("rejected") else "")
                   + f", субагентов {ps.get('subagents', 0)}, откатов {ps.get('rollbacks', 0)}"
                   + (f", отказов run_plan {ps['refused']}" if ps.get("refused") else ""))
        if ps.get("writes", 0) >= 3 and not ps.get("subagents"):
            out.append("план переписывался, но ни разу не исполнялся — модель крутится на планировании")
        for node in (result.get("plan_nodes") or []):
            if node.get("status") != "done":
                out.append(f"  ⊘ {node.get('id')} {node.get('title')} — {node.get('status')}")
    elif calls:
        out.append("план не использовался (plan_write не вызывался)")
    trace = result.get("trace") or []
    if trace:
        out.append("последние вызовы:")
        for entry in trace[-6:]:
            mark = "✓" if entry.get("ok") else "✗"
            out.append(f"  {mark} шаг {entry.get('step')} {entry.get('tool')}"
                       f"({entry.get('args', '')}) → {entry.get('out', '')[:70]}")
    return out


def report(results: list[dict], baseline: dict | None = None) -> None:
    width = max(len(r["name"]) for r in results)
    has_partial = any(r.get("partial_total") for r in results)
    print(f"\n{'задача'.ljust(width)}  итог   шагов  токены(in/out)  сек"
          + ("   цели" if has_partial else ""))
    for r in results:
        mark = "✓" if r["passed"] else "✗"
        tokens = f"{r.get('tokens_in', 0)}/{r.get('tokens_out', 0)}"
        line = (f"{r['name'].ljust(width)}  {mark}    {str(r.get('steps', '-')).rjust(5)}"
                f"  {tokens.rjust(14)}  {str(r['seconds']).rjust(5)}")
        if has_partial:
            goals = (f"{r['partial']}/{r['partial_total']}" if r.get("partial_total") else "-")
            line += "  " + goals.rjust(5)
        if baseline:
            was = next((b for b in baseline.get("results", []) if b["name"] == r["name"]), None)
            if was and was["passed"] != r["passed"]:
                line += "  ← лучше" if r["passed"] else "  ← хуже"
            elif was and r.get("partial_total") and was.get("partial") != r.get("partial"):
                line += f"  ← цели {was.get('partial', '?')}→{r['partial']}"
        print(line)
        if not r["passed"] and r["reason"]:
            print(f"{' ' * width}     {r['reason']}")
        if r.get("partial_failed"):
            print(f"{' ' * width}     не достигнуто: {', '.join(r['partial_failed'])}")
        if not r["passed"]:
            for line in diagnose(r):
                print(f"{' ' * width}     {line}")
        plan_stats = r.get("plan_stats") or {}
        if plan_stats.get("subagents"):
            print(f"{' ' * width}     план: субагентов {plan_stats['subagents']}, "
                  f"проверок {plan_stats.get('accept_runs', 0)}, "
                  f"откатов {plan_stats.get('rollbacks', 0)}")
    passed = sum(1 for r in results if r["passed"])
    total_seconds = sum(r["seconds"] for r in results)
    steps = [r["steps"] for r in results if isinstance(r.get("steps"), int)]
    goals_done = sum(r.get("partial", 0) for r in results)
    goals_total = sum(r.get("partial_total", 0) for r in results)
    print(f"\nпрошло {passed}/{len(results)} · время {total_seconds:.0f}s"
          + (f" · шагов медиана {statistics.median(steps):.0f}" if steps else "")
          + (f" · частичных целей {goals_done}/{goals_total}" if goals_total else ""))
    if baseline:
        was = sum(1 for b in baseline.get("results", []) if b["passed"])
        print(f"baseline: {was}/{len(baseline.get('results', []))} "
              f"({baseline.get('model', '?')}) → сейчас {passed}/{len(results)}")


def verify_tasks() -> int:
    """Проверяет сами задачи: до работы агента checks должны падать."""
    bad = 0
    for task in TASKS:
        workspace = Path(tempfile.mkdtemp(prefix=f"loca-verify-{task['name']}-"))
        try:
            materialize(task, workspace)
            for rel in task["files"]:
                if rel.endswith(".py"):
                    src = (workspace / rel).read_text(encoding="utf-8")
                    try:
                        compile(src, rel, "exec")
                    except SyntaxError as e:
                        print(f"✗ {task['name']}: {rel} не компилируется: {e}")
                        bad += 1
            write_verify(task, workspace)
            ok, _reason = run_checks(task, workspace)
            if not task.get("checks"):
                status = "нет checks (только expect_output)"
                if not task.get("expect_output"):
                    print(f"✗ {task['name']}: ни checks, ни expect_output")
                    bad += 1
            elif ok:
                print(f"✗ {task['name']}: checks проходят ДО работы агента — задача пустая")
                bad += 1
                status = "уже решена"
            else:
                status = "ок, падает как надо"
            if task.get("partial"):
                done, total, _failed = score_partial(task, workspace)
                status += f" · частичные цели до агента {done}/{total}"
                if done == total:
                    print(f"✗ {task['name']}: все частичные цели достигнуты ДО агента")
                    bad += 1
            if "hard" in task.get("tags", []) and len(task.get("partial") or []) < 3:
                print(f"✗ {task['name']}: помечена hard, но частичных целей меньше трёх")
                bad += 1
            print(f"{'✓' if not ok or not task.get('checks') else '✗'} {task['name']}: {status}")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
    print(f"\nзадач: {len(TASKS)}, проблем: {bad}")
    return 1 if bad else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="мини-бенчмарк loca")
    p.add_argument("--tasks", help="через запятую: fix_typo,write_test — или группа: "
                                   + ", ".join(sorted(GROUPS)))
    p.add_argument("-m", "--model")
    p.add_argument("--ctx", type=int)
    p.add_argument("--host")
    p.add_argument("--backend", choices=["ollama", "llamacpp"])
    p.add_argument("--think", action="store_true")
    p.add_argument("--loca", help="команда запуска (по умолчанию python3 -m loca из репозитория)")
    p.add_argument("--timeout", type=int, default=420, help="жёсткий таймаут на задачу")
    p.add_argument("--time-scale", type=float, default=1.0, dest="time_scale",
                   help="умножить таймауты задач (медленное железо: CPU — 10–15)")
    p.add_argument("--margin", type=int, default=45,
                   help="за сколько секунд до таймаута агент останавливается сам (0 — не мешать)")
    p.add_argument("--out", help="куда записать JSON с результатами")
    p.add_argument("--baseline", help="JSON предыдущего прогона для сравнения")
    p.add_argument("--only-failed", metavar="FILE", dest="only_failed",
                   help="прогнать только задачи, упавшие в этом JSON (можно тот же, что baseline)")
    p.add_argument("--keep", action="store_true", help="не удалять временные каталоги")
    p.add_argument("--show-output", action="store_true", help="печатать вывод агента для упавших")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--verify-tasks", action="store_true", help="проверить задачи без модели")
    args = p.parse_args(argv)

    if args.list:
        for task in TASKS:
            tag = "hard" if "hard" in task.get("tags", []) else "    "
            print(f"{task['name']:20} {tag}  {task['prompt'][:70]}")
        return 0
    if args.verify_tasks:
        return verify_tasks()

    selected = by_name(args.tasks.split(",")) if args.tasks else list(TASKS)
    if args.only_failed:
        try:
            previous = json.loads(Path(args.only_failed).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"не прочитать {args.only_failed}: {e}")
        failed = {r["name"] for r in previous.get("results", []) if not r.get("passed")}
        if not failed:
            print(f"в {args.only_failed} все задачи прошли — нечего перезапускать")
            return 0
        selected = [t for t in selected if t["name"] in failed]
        print("перезапуск упавших: " + ", ".join(t["name"] for t in selected))
    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"baseline не прочитан: {e}")

    results = []
    for i, task in enumerate(selected, 1):
        print(f"[{i}/{len(selected)}] {task['name']}…", flush=True)
        results.append(run_task(task, args))
        last = results[-1]
        print(f"  {'✓' if last['passed'] else '✗'} {last['seconds']}s "
              f"{last.get('steps', '?')} шагов {last['reason']}", flush=True)

    report(results, baseline)
    if args.out:
        payload = {"model": args.model or "(из конфига)", "ctx": args.ctx,
                   "when": time.strftime("%Y-%m-%d %H:%M"), "results": results}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"результаты: {args.out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
