from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..permissions import Permissions
    from ..plan import Plan
    from ..state import Vars
    from ..ui import UI


class ToolError(Exception):
    """Raised by tools; message is sent back to the model."""


@dataclass
class ToolContext:
    cwd: Path
    cfg: dict
    ui: "UI"
    permissions: "Permissions"
    read_files: dict = field(default_factory=dict)   # path -> mtime when read
    plan: "Plan" = None                              # type: ignore[assignment]  граф подзадач
    plan_stats: dict = field(default_factory=dict)   # метрики исполнения плана
    vars: "Vars" = None                              # type: ignore[assignment]
    snapshots: Any = None       # loca.snapshots.Snapshots — для /undo
    edited: bool = False        # a file was changed in this turn
    touched: set = field(default_factory=set)   # какие файлы правились в этом ходе
    deadline: float = 0.0       # абсолютный дедлайн хода (time.time()), 0 — без лимита
    budget_hit: bool = False    # ход остановлен по времени
    tests_run: int = 0          # run_tests calls in this turn
    tests_passed: bool = False  # verdict of the last run_tests
    # set by the CLI: builds a subagent and runs it, returns its final answer
    spawn_subagent: Callable[..., str] | None = None
    # set by the CLI: rebuild the system prompt (after a mode / memory change)
    refresh_system: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if self.plan is None:
            from ..plan import Plan
            self.plan = Plan()
        for key in ("subagents", "rollbacks", "accept_runs", "writes", "rejected", "refused"):
            self.plan_stats.setdefault(key, 0)
        self.plan_stats.setdefault("seconds", 0.0)
        if self.vars is None:
            from ..state import Vars
            self.vars = Vars(limit=int(self.cfg.get("vars_limit", 4000)),
                             max_vars=int(self.cfg.get("max_vars", 40)))

    def resolve(self, path: str) -> Path:
        if not path:
            raise ToolError("path is required")
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.cwd / p
        return p.resolve()

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.cwd))
        except ValueError:
            return str(p)

    def inside_cwd(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.cwd.resolve())
            return True
        except ValueError:
            return False


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}, "required": []}
    read_only: bool = True     # read-only tools never ask for permission
    kind: str = "read"         # read | edit | exec | other  (for permission modes)
    # {{var}} expansion: None = every string argument; whole_vars = only when the
    # argument is exactly one placeholder (keeps {{...}} templates in file content safe)
    expand_vars: tuple | None = None
    expand_vars_whole: tuple = ()

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}

    def summary(self, args: dict) -> str:
        """One-line description of a call for the transcript."""
        for key in ("path", "command", "pattern", "description", "url"):
            if key in args:
                return str(args[key])[:100]
        return ", ".join(f"{k}={str(v)[:30]}" for k, v in args.items())

    def permission_key(self, args: dict) -> str:
        """String matched against allow/deny rules, e.g. 'bash(git status)'."""
        return self.name

    def preview(self, args: dict, ctx: ToolContext) -> str | None:
        """Optional extra text shown when asking for permission (e.g. diff)."""
        return None

    def validate(self, args: dict) -> None:
        props = self.parameters.get("properties", {})
        for key in self.parameters.get("required", []):
            if key not in args or args[key] is None:
                raise ToolError(f"missing required argument '{key}'. "
                                f"Expected arguments: {list(props)}")
        for key, spec in props.items():
            if key not in args:
                continue
            t = spec.get("type")
            v = args[key]
            # small models often send numbers/bools as strings — coerce
            if t == "integer" and isinstance(v, str) and v.strip().lstrip("-").isdigit():
                args[key] = int(v)
            elif t == "boolean" and isinstance(v, str):
                args[key] = v.strip().lower() in ("true", "1", "yes")
            elif t == "string" and not isinstance(v, str):
                args[key] = str(v) if not isinstance(v, (dict, list)) else v

    def run(self, args: dict, ctx: ToolContext) -> str:  # pragma: no cover
        raise NotImplementedError


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    cut = len(text) - limit
    return f"{text[:head]}\n\n… [{cut} chars truncated] …\n\n{text[-tail:]}"


def obj(properties: dict[str, Any], required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}
