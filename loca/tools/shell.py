"""bash tool. Keeps the working directory between calls (cd persists)."""
from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

from .base import Tool, ToolError, obj, truncate

_MARK = "__LOCA_CWD__"
# команда явно гоняет тесты — значит, это работа для run_tests
TEST_CMD_RE = re.compile(r"\b(pytest|unittest|nosetests|npm\s+(run\s+)?test|yarn\s+test|"
                         r"pnpm\s+test|cargo\s+test|go\s+test|make\s+test)\b")


class Bash(Tool):
    name = "bash"
    description = ("Run a shell command (bash, macOS). Use for git, tests, builds, installing packages. "
                   "Do not use it to read/edit files (use read_file/edit_file) or to search "
                   "(use grep/glob). Avoid interactive commands; add flags like -y. "
                   "`cd` persists between calls.")
    parameters = obj({
        "command": {"type": "string"},
        "timeout": {"type": "integer", "description": "Seconds, default 120, max 600"},
    }, ["command"])
    read_only = False
    kind = "exec"

    def permission_key(self, args):
        return f"bash({args.get('command', '').strip()})"

    def run(self, args, ctx):
        command = args["command"].strip()
        if not command:
            raise ToolError("command is empty")
        timeout = min(int(args.get("timeout") or 120), 600)
        script = f'{command}\n__rc=$?; printf "\\n{_MARK}%s" "$PWD"; exit $__rc'
        env = {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat", "TERM": "dumb",
               "CI": "1", "NO_COLOR": "1"}
        proc = subprocess.Popen(
            ["/bin/bash", "-c", script], cwd=ctx.cwd, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, errors="replace")
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            return truncate(out or "", ctx.cfg["max_tool_output"]) + f"\n[killed: timeout after {timeout}s]"
        except KeyboardInterrupt:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        out = out or ""
        if _MARK in out:
            out, _, new_cwd = out.rpartition(_MARK)
            new_cwd = new_cwd.strip()
            if new_cwd and Path(new_cwd).is_dir() and Path(new_cwd) != ctx.cwd:
                ctx.cwd = Path(new_cwd)
                out += f"\n[cwd is now {new_cwd}]"
        out = out.rstrip()
        result = truncate(out, ctx.cfg["max_tool_output"]) if out else "(no output)"
        if TEST_CMD_RE.search(command):
            # 14 вызовов bash с ручным pytest в одном прогоне бенчмарка: run_tests знает
            # команду проекта, считается проверкой и печатает короткий вердикт
            result += ("\n\nПодсказка: для тестов вызывайте run_tests — он сам знает команду "
                       "проекта, сокращает вывод и отмечает прогон как проверку.")
        if proc.returncode != 0:
            result += f"\n[exit code {proc.returncode}]"
        return result
