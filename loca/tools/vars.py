"""Variable tools: save intermediate results and reuse them as {{name}}."""
from __future__ import annotations

from ..state import VarError
from .base import Tool, ToolError, obj


class SetVar(Tool):
    name = "set_var"
    description = ("Save a value under a short name so later steps can reuse it without "
                   "re-reading or re-deriving it. Use it for: the test command, key file "
                   "paths, the plan, an error message, a snippet you will paste elsewhere. "
                   "Afterwards write {{name}} inside any tool argument to insert the value.")
    parameters = obj({
        "name": {"type": "string", "description": "Identifier, e.g. test_cmd or main_file"},
        "value": {"type": "string", "description": "Value to remember"},
    }, ["name", "value"])
    read_only = True          # writing to memory never needs permission
    kind = "other"

    def summary(self, args):
        return f"{args.get('name')} = {str(args.get('value', ''))[:60]!r}"

    def run(self, args, ctx):
        try:
            value = ctx.vars.set(args["name"], args["value"])
        except VarError as e:
            raise ToolError(str(e))
        known = ", ".join(ctx.vars.names())
        return (f"Saved {args['name']} ({len(value)} chars). Insert it later as "
                f"{{{{{args['name']}}}}}. Variables now: {known}")


class GetVar(Tool):
    name = "get_var"
    description = ("Read back a variable saved with set_var. Prefer writing {{name}} directly "
                   "in a tool argument; use this only when you need to see the value itself.")
    parameters = obj({"name": {"type": "string"}}, ["name"])
    read_only = True
    kind = "other"

    def summary(self, args):
        return str(args.get("name"))

    def run(self, args, ctx):
        value = ctx.vars.get(args["name"])
        if value is None:
            known = ", ".join(ctx.vars.names()) or "(none saved)"
            raise ToolError(f"No variable '{args['name']}'. Saved variables: {known}")
        return value


class DelVar(Tool):
    name = "del_var"
    description = "Forget a variable that is no longer needed."
    parameters = obj({"name": {"type": "string"}}, ["name"])
    read_only = True
    kind = "other"

    def summary(self, args):
        return str(args.get("name"))

    def run(self, args, ctx):
        if not ctx.vars.delete(args["name"]):
            raise ToolError(f"No variable '{args['name']}'.")
        return f"Deleted {args['name']}. Variables now: {', '.join(ctx.vars.names()) or '(none)'}"
