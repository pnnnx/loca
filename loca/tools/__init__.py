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


def main_tools() -> list[Tool]:
    return [ReadFile(), ReadSymbol(), RepoMap(), EditFile(), WriteFile(), ListDir(), Glob(),
            Grep(), Bash(), RunTests(), WriteTests(), PlanWrite(), RunPlan(), Task(), ExitPlanMode(),
            SetVar(), GetVar(), DelVar(), WebFetch()]


def subagent_tools(kind: str = "research") -> list[Tool]:
    """Tools for a subagent: research = read-only, code = everything but nesting."""
    if kind == "code":
        return [ReadFile(), ReadSymbol(), RepoMap(), EditFile(), WriteFile(), ListDir(), Glob(),
                Grep(), Bash(), RunTests(), WriteTests(), SetVar(), GetVar()]
    return [cls() for cls in READ_ONLY_TOOLS] + [SetVar(), GetVar()]


__all__ = ["Tool", "ToolContext", "ToolError", "truncate", "main_tools", "subagent_tools",
           "detect_test_command", "outline"]
