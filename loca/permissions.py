"""Permission checks for tool calls.

Modes:
  default       ask before edits, commands, network
  accept-edits  file edits inside the project are auto-approved
  plan          read-only: no edits, only safe commands
  yolo          everything auto-approved except obviously dangerous commands
"""
from __future__ import annotations

import fnmatch
import re
import shlex
import sys
from typing import TYPE_CHECKING

from .ui import bold, dim, yellow

if TYPE_CHECKING:
    from .tools.base import Tool, ToolContext

MODES = ("default", "accept-edits", "plan", "yolo")

SAFE_COMMANDS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "echo", "which", "whoami", "date", "tree",
    "rg", "grep", "file", "stat", "du", "df", "uname", "env", "printenv", "sort", "uniq",
    "diff", "basename", "dirname", "realpath", "true", "type",
}
SAFE_SUBCOMMANDS = {
    "git": {"status", "diff", "log", "show", "branch", "blame", "remote", "rev-parse", "ls-files"},
    "npm": {"ls", "view", "outdated"},
    "pip": {"list", "show", "freeze"},
    "python": {"--version"}, "python3": {"--version"}, "node": {"--version"},
    "brew": {"list", "info"},
}
DANGEROUS = [
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME|\*)(\s|$)",
    r"\bsudo\b", r"\bmkfs\b", r"\bdd\s+if=", r":\(\)\s*\{", r"\bshutdown\b", r"\breboot\b",
    r"(curl|wget)[^|]*\|\s*(ba|z)?sh", r"\bgit\s+push\b.*(--force|-f)\b", r"\bchmod\s+-R\s+777\b",
    r">\s*/dev/(disk|sd)", r"\bdiskutil\s+erase",
]


def is_dangerous(command: str) -> bool:
    return any(re.search(p, command) for p in DANGEROUS)


def is_safe_command(command: str) -> bool:
    if re.search(r"[;<>`]|\$\(|&(?!&)", command):
        return False
    for segment in re.split(r"\|\||&&|\|", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        if not words:
            return False
        head = words[0]
        if head == "find":
            if any(w in ("-delete", "-exec", "-execdir", "-ok") for w in words):
                return False
            continue
        if head in SAFE_COMMANDS:
            continue
        subs = SAFE_SUBCOMMANDS.get(head)
        if subs and len(words) > 1 and words[1] in subs:
            continue
        return False
    return True


def rule_matches(rule: str, key: str) -> bool:
    rule = rule.strip()
    if "(" not in rule:
        return key.split("(", 1)[0] == rule
    return fnmatch.fnmatchcase(key, rule)


def suggest_rule(tool: "Tool", args: dict) -> str:
    if tool.name == "bash":
        try:
            words = shlex.split(args.get("command", ""))
        except ValueError:
            words = args.get("command", "").split()
        if not words:
            return "bash"
        multi = set(SAFE_SUBCOMMANDS) | {"npm", "npx", "yarn", "pnpm", "uv", "cargo", "go",
                                         "make", "docker", "poetry"}
        if words[0] in multi and len(words) > 1:
            return f"bash({words[0]} {words[1]}*)"
        return f"bash({words[0]}*)"
    return tool.permission_key(args) if tool.name == "web_fetch" else tool.name


class Permissions:
    def __init__(self, cfg: dict, interactive: bool = True):
        self.mode = cfg.get("permission_mode", "default")
        if self.mode not in MODES:
            self.mode = "default"
        self.allow = list(cfg.get("allow", []))
        self.deny = list(cfg.get("deny", []))
        self.session_allow: list[str] = []
        self.interactive = interactive and sys.stdin.isatty()

    def check(self, tool: "Tool", args: dict, ctx: "ToolContext") -> tuple[bool, str | None]:
        """Returns (allowed, feedback_for_model)."""
        key = tool.permission_key(args)
        if any(rule_matches(r, key) for r in self.deny):
            return False, f"Blocked by user deny rule for {key}."

        command = args.get("command", "") if tool.name == "bash" else ""
        dangerous = bool(command) and is_dangerous(command)

        if tool.read_only and not dangerous:
            return True, None

        if self.mode == "plan":
            if tool.name == "bash" and is_safe_command(command):
                return True, None
            return False, ("Plan mode is active: you may only read and explore. "
                           "Present your plan to the user instead of making changes.")

        outside = False
        if "path" in args:
            try:
                outside = not ctx.inside_cwd(ctx.resolve(str(args["path"])))
            except Exception:  # noqa: BLE001
                outside = True

        if not dangerous and not outside:
            if any(rule_matches(r, key) for r in self.allow + self.session_allow):
                return True, None
            if self.mode == "yolo":
                return True, None
            if self.mode == "accept-edits" and tool.kind == "edit":
                return True, None
            if tool.name == "bash" and is_safe_command(command):
                return True, None
        elif self.mode == "yolo" and outside and not dangerous:
            return True, None

        return self._ask(tool, args, ctx, dangerous, outside)

    def _ask(self, tool, args, ctx, dangerous, outside) -> tuple[bool, str | None]:
        if not self.interactive:
            return False, (f"Permission for {tool.name} was not granted (non-interactive run). "
                           "Tell the user what you wanted to do.")
        preview = tool.preview(args, ctx)
        print()
        if preview:
            print(preview)
        if tool.name == "bash":
            print(f"  {bold('$')} {args.get('command')}")
        if dangerous:
            print(yellow("  ⚠ команда выглядит опасной"))
        if outside:
            print(yellow("  ⚠ путь вне папки проекта"))
        rule = suggest_rule(tool, args)
        prompt = (f"  Разрешить {bold(tool.name)}? "
                  f"[y] да · [a] всегда «{rule}» · [n] нет · {dim('или текст — указание модели')}: ")
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False, "User interrupted and denied the action. Stop and wait for instructions."
        low = answer.lower()
        if low in ("y", "yes", "д", "да") or (low == "" and not dangerous):
            return True, None
        if low in ("a", "always", "в", "всегда"):
            if not dangerous:
                self.session_allow.append(rule)
            return True, None
        if low in ("n", "no", "н", "нет"):
            return False, "User denied this action. Ask what they want instead, or try another approach."
        return False, f"User denied this action with feedback: {answer}"
