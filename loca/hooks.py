"""Хуки: команды проекта, которые loca запускает вокруг своих действий.

Настройка в config.json:

    "hooks": {
      "after_edit":  ["ruff format {file}", "ruff check --quiet {file}"],
      "after_tests": ["git --no-pager diff --stat"],
      "stop":        ["python3 -m pytest -q"]
    }

`{file}` подставляется путём изменённого файла (только для after_edit).
Вывод команд возвращается модели: форматтер и линтер дают ей обратную связь
сразу после правки, а не через пять шагов.
"""
from __future__ import annotations

import os
import shlex
import subprocess

EVENTS = ("after_edit", "after_tests", "stop")
MAX_HOOK_OUTPUT = 800


def commands(cfg: dict, event: str) -> list[str]:
    hooks = cfg.get("hooks") or {}
    value = hooks.get(event) or []
    if isinstance(value, str):
        value = [value]
    return [str(c) for c in value if str(c).strip()]


def enabled(cfg: dict) -> bool:
    return any(commands(cfg, event) for event in EVENTS)


def _run_one(command: str, cwd, timeout: int) -> tuple[int, str]:
    env = {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat", "TERM": "dumb",
           "CI": "1", "NO_COLOR": "1"}
    try:
        proc = subprocess.run(["/bin/bash", "-c", command], cwd=cwd, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"[хук прерван: таймаут {timeout}s]"
    except OSError as e:
        return 127, f"[хук не запустился: {e}]"
    out = (proc.stdout or "").strip()
    if len(out) > MAX_HOOK_OUTPUT:
        out = out[:MAX_HOOK_OUTPUT] + "\n… (вывод хука обрезан)"
    return proc.returncode, out


def run(event: str, ctx, file=None) -> str:
    """Выполняет команды события. Возвращает текст для модели ('' — если молчать)."""
    cmds = commands(ctx.cfg, event)
    if not cmds:
        return ""
    timeout = int(ctx.cfg.get("hook_timeout", 120))
    before = None
    if file is not None:
        try:
            before = os.stat(file).st_mtime
        except OSError:
            before = None
    lines = []
    for cmd in cmds:
        if "{file}" in cmd:
            if file is None:
                continue
            command = cmd.replace("{file}", shlex.quote(str(file)))
        else:
            command = cmd
        code, out = _run_one(command, ctx.cwd, timeout)
        head = f"[hook {event}] {command}"
        if code == 0:
            lines.append(head + (f" → ok\n{out}" if out else " → ok"))
        else:
            lines.append(head + f" → exit {code}" + (f"\n{out}" if out else ""))
    # форматтер меняет файл: без обновления mtime следующий edit_file упрётся
    # в «changed on disk», а модель не поймёт, почему
    if file is not None:
        try:
            after = os.stat(file).st_mtime
        except OSError:
            after = None
        if after is not None and str(file) in ctx.read_files:
            ctx.read_files[str(file)] = after
        if before is not None and after is not None and after != before:
            lines.append("Файл изменён хуком — перечитайте его перед следующей правкой.")
    return "\n".join(lines)
