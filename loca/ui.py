"""Terminal output helpers: colors, spinner, diffs. Stdlib only."""
from __future__ import annotations

import difflib
import itertools
import os
import sys
import threading
import time

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str):
    def wrap(text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if _COLOR else text
    return wrap


dim, bold, red, green, yellow, blue, magenta, cyan = (
    _c("2"), _c("1"), _c("31"), _c("32"), _c("33"), _c("34"), _c("35"), _c("36"))


class Spinner:
    """Shows a spinner until stop() is called (e.g. on first streamed token)."""

    def __init__(self, text: str = "думаю"):
        self.text = text
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Spinner":
        if not _COLOR:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def _spin(self) -> None:
        start = time.time()
        for frame in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{cyan(frame)} {dim(f'{self.text}… {time.time() - start:.0f}s')}")
            sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def stop(self) -> None:
        if self._thread and not self._stop.is_set():
            self._stop.set()
            self._thread.join()


class UI:
    """All user-facing output goes through here so subagents can be quieter."""

    def __init__(self, quiet: bool = False, prefix: str = ""):
        self.quiet = quiet
        self.prefix = prefix
        self._in_think = False
        self._streamed = False

    # --- streaming -------------------------------------------------------
    def begin_stream(self) -> None:
        self._streamed = False
        self._in_think = False

    def token(self, text: str) -> None:
        if self.quiet:
            return
        if self._in_think:
            sys.stdout.write("\n")
            self._in_think = False
        sys.stdout.write(text)
        sys.stdout.flush()
        self._streamed = True

    def think_token(self, text: str) -> None:
        if self.quiet:
            return
        if not self._in_think:
            sys.stdout.write(dim("✻ "))
            self._in_think = True
        sys.stdout.write(dim(text))
        sys.stdout.flush()

    def end_stream(self) -> None:
        if self.quiet:
            return
        if self._streamed or self._in_think:
            sys.stdout.write("\n")
            sys.stdout.flush()

    # --- events ----------------------------------------------------------
    def tool_call(self, name: str, summary: str) -> None:
        if self.quiet:
            return
        print(f"{self.prefix}{green('●')} {bold(name)}({summary})")

    def tool_result(self, text: str, error: bool = False) -> None:
        if self.quiet:
            return
        lines = text.strip().splitlines() or [""]
        head = lines[0][:150]
        more = f" {dim(f'(+{len(lines) - 1} строк)')}" if len(lines) > 1 else ""
        color = red if error else dim
        print(f"{self.prefix}  {dim('⎿')} {color(head)}{more}")

    def info(self, text: str) -> None:
        if not self.quiet:
            print(f"{self.prefix}{cyan(text)}")

    def warn(self, text: str) -> None:
        print(f"{self.prefix}{yellow(text)}")

    def error(self, text: str) -> None:
        print(f"{self.prefix}{red(text)}")


def render_diff(old: str, new: str, path: str, max_lines: int = 60) -> str:
    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="", n=2))
    out = []
    for line in diff[:max_lines]:
        if line.startswith("+") and not line.startswith("+++"):
            out.append(green(line))
        elif line.startswith("-") and not line.startswith("---"):
            out.append(red(line))
        elif line.startswith("@@"):
            out.append(cyan(line))
        else:
            out.append(dim(line))
    if len(diff) > max_lines:
        out.append(dim(f"… ещё {len(diff) - max_lines} строк диффа"))
    return "\n".join(out)
