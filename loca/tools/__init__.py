from __future__ import annotations

from .agentic import ExitPlanMode, PlanWrite, RunPlan, Task
from .base import Tool, ToolContext, ToolError, truncate
from .codemap import ReadSymbol, RepoMap, outline
from .fs import EditFile, Glob, Grep, ListDir, ReadFile, WriteFile
from .shell import Bash
from .tests import RunTests, detect_test_command
from .testgen import WriteTests
from .vars import DelVar, GetVar, SetVar
from .web import WebFetch

READ_ONLY_TOOLS = [ReadFile, ReadSymbol, RepoMap, ListDir, Glob, Grep]


def index_tools(cfg: dict | None) -> list[Tool]:
    """deps, impact и review_diff — только с флагом codeindex (бета, A/B в T-20)."""
    from .codeindex import Deps, Impact
    from .review import ReviewDiff
    return [Deps(), Impact(), ReviewDiff()] if cfg and cfg.get("codeindex") else []


def main_tools(cfg: dict | None = None) -> list[Tool]:
    return [ReadFile(), ReadSymbol(), RepoMap(), EditFile(), WriteFile(), ListDir(), Glob(),
            Grep(), Bash(), RunTests(), WriteTests(), PlanWrite(), RunPlan(), Task(), ExitPlanMode(),
            SetVar(), GetVar(), DelVar(), WebFetch()] + index_tools(cfg)


def subagent_tools(kind: str = "research", cfg: dict | None = None) -> list[Tool]:
    """Tools for a subagent: research = read-only, code = everything but nesting."""
    if kind == "code":
        return [ReadFile(), ReadSymbol(), RepoMap(), EditFile(), WriteFile(), ListDir(), Glob(),
                Grep(), Bash(), RunTests(), WriteTests(), SetVar(), GetVar()] + index_tools(cfg)
    return [cls() for cls in READ_ONLY_TOOLS] + [SetVar(), GetVar()] + index_tools(cfg)


__all__ = ["Tool", "ToolContext", "ToolError", "truncate", "main_tools", "subagent_tools",
           "detect_test_command", "outline"]
