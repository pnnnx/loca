"""run_tests: find the project's test command, run it, report a short verdict."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from .base import Tool, ToolError, obj, truncate

# (marker check, command builder) in priority order
_PYTEST_MARKERS = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py")


def _has_pytest_config(root: Path) -> bool:
    for name in _PYTEST_MARKERS:
        p = root / name
        if not p.exists():
            continue
        if name in ("pytest.ini", "conftest.py"):
            return True
        try:
            if "pytest" in p.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def _npm_runner(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
        return "pnpm"
    if (root / "yarn.lock").exists() and shutil.which("yarn"):
        return "yarn"
    return "npm"


def _make_has_test(root: Path) -> bool:
    mk = root / "Makefile"
    if not mk.exists():
        return False
    try:
        return bool(re.search(r"^test\s*:", mk.read_text(encoding="utf-8", errors="replace"), re.M))
    except OSError:
        return False


def pytest_available() -> bool:
    """Есть ли pytest у ТОГО python, которым мы его запустим.

    После brew-обновления питона (например на 3.14) pytest остаётся у старого
    интерпретатора, и «прогон тестов» превращается в «No module named pytest» —
    агент винит в этом свою правку. Лучше сразу взять unittest из stdlib.
    """
    import importlib.util
    try:
        return importlib.util.find_spec("pytest") is not None
    except (ImportError, ValueError):
        return False


def detect_test_command(root: Path, pytest_ok: bool | None = None) -> tuple[str, str] | None:
    """Returns (command, framework) or None when no test setup is recognised."""
    if (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pkg = {}
        if isinstance(pkg.get("scripts"), dict) and pkg["scripts"].get("test"):
            runner = _npm_runner(root)
            return (f"{runner} test" if runner != "npm" else "npm test --silent"), "node"
    if (root / "Cargo.toml").exists():
        return "cargo test", "cargo"
    if (root / "go.mod").exists():
        return "go test ./...", "go"
    tests_dir = next((d for d in ("tests", "test") if (root / d).is_dir()), None)
    if _has_pytest_config(root) or tests_dir:
        if pytest_ok is None:
            pytest_ok = pytest_available()
        if pytest_ok:
            return "python3 -m pytest -q", "pytest"
        if tests_dir:
            # pytest недоступен этому питону — stdlib-unittest всегда на месте
            return f"python3 -m unittest discover -s {tests_dir} -t . -q", "unittest"
        return "python3 -m pytest -q", "pytest"
    if _make_has_test(root):
        return "make test", "make"
    return None


_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed)"
    r"|Tests:\s+(?:(\d+)\s+failed,\s+)?(\d+)\s+passed"
    r"|test result:\s+(ok|FAILED)\.\s+(\d+)\s+passed;\s+(\d+)\s+failed", re.I)


def verdict(output: str, returncode: int) -> str:
    tail = output[-2000:]
    hits = [m.group(0) for m in _COUNT_RE.finditer(tail)]
    line = "; ".join(hits[-3:]) if hits else ""
    status = "PASSED" if returncode == 0 else "FAILED"
    return f"[tests {status}{': ' + line if line else ''} · exit code {returncode}]"


class RunTests(Tool):
    name = "run_tests"
    description = ("Run the project's test suite and report pass/fail. Detects pytest, "
                   "npm/pnpm/yarn test, cargo test, go test or make test automatically. "
                   "Call this after every code change; pass `target` to run one test file "
                   "first (faster), then the whole suite. Use `command` only to override.")
    parameters = obj({
        "target": {"type": "string", "description": "Optional test file/dir or package to limit the run"},
        "pattern": {"type": "string", "description": "Optional name filter (pytest -k / cargo/go test name)"},
        "command": {"type": "string", "description": "Explicit command, overrides detection"},
        "timeout": {"type": "integer", "description": "Seconds, default 300, max 900"},
    }, [])
    read_only = False
    kind = "exec"

    # ------------------------------------------------------------------
    def build(self, args: dict, ctx) -> tuple[str, str]:
        explicit = (args.get("command") or "").strip() or (ctx.cfg.get("test_command") or "").strip()
        if explicit:
            command, framework = explicit, "config"
        else:
            found = detect_test_command(ctx.cwd)
            if not found:
                raise ToolError(
                    "No test setup found in this project (looked for pytest, package.json "
                    "scripts.test, Cargo.toml, go.mod, Makefile). Write a test first, or pass "
                    "`command` explicitly, or set \"test_command\" in .loca/config.json.")
            command, framework = found
        target = (args.get("target") or "").strip()
        pattern = (args.get("pattern") or "").strip()
        if target:
            if framework == "node" and " -- " not in command:
                command += f" -- {target}"
            else:
                command += f" {target}"
        if pattern:
            if framework in ("pytest", "config"):
                command += f" -k {pattern!r}"
            elif framework == "unittest":
                command += f" -k {pattern}"
            elif framework == "cargo":
                command += f" {pattern}"
            elif framework == "go":
                command += f" -run {pattern}"
            elif framework == "node":
                command += (" " if " -- " in command else " -- ") + f"-t {pattern}"
        return command, framework

    def summary(self, args):
        parts = [str(args[k]) for k in ("command", "target", "pattern") if args.get(k)]
        return " ".join(parts) or "весь набор"

    def permission_key(self, args):
        return f"run_tests({(args.get('command') or args.get('target') or '').strip()})"

    def run(self, args, ctx):
        command, framework = self.build(args, ctx)
        timeout = min(int(args.get("timeout") or 300), 900)
        env = {**os.environ, "PAGER": "cat", "TERM": "dumb", "CI": "1", "NO_COLOR": "1",
               "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(["/bin/bash", "-c", command], cwd=ctx.cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, errors="replace")
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            head = truncate(out or "", ctx.cfg["max_tool_output"])
            return f"$ {command}\n{head}\n[tests TIMED OUT after {timeout}s]"
        except KeyboardInterrupt:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        out = (out or "").rstrip()
        ctx.tests_run += 1
        ctx.tests_passed = proc.returncode == 0
        limit = ctx.cfg["max_tool_output"]
        body = truncate(out, limit) if out else "(no output)"
        tail = verdict(out, proc.returncode)
        if proc.returncode != 0:
            tail += "\nFix the failures, then run run_tests again. Do not report success until it passes."
        return f"$ {command}\n{body}\n{tail}"
