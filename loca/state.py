"""Session variables: a scratchpad where the agent keeps intermediate results.

Small models forget values or re-derive them with extra tool calls. A variable
lets one step store a result (a path, the test command, an error message, a
chosen approach) under a short name, and any later step reuse it as {{name}}
inside a tool argument — without keeping the whole text in the context window.
Variables survive context compaction and are saved with the session.
"""
from __future__ import annotations

import json
import re

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,47}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,47})\s*\}\}")
WHOLE_RE = re.compile(r"^\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,47})\s*\}\}\s*$")


class VarError(ValueError):
    """Bad variable name / too many variables. Message goes back to the model."""


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    if value is None:
        return ""
    return str(value)


class Vars:
    def __init__(self, limit: int = 4000, max_vars: int = 40):
        self.limit = limit
        self.max_vars = max_vars
        self.data: dict[str, str] = {}

    # ------------------------------------------------------------------ crud
    def set(self, name: str, value) -> str:
        name = str(name or "").strip()
        if not NAME_RE.match(name):
            raise VarError("variable name must be a short identifier "
                           "(letters, digits, '_', starting with a letter), e.g. test_cmd")
        text = _stringify(value)
        if len(text) > self.limit:
            text = text[: self.limit] + f"\n… (value truncated to {self.limit} chars)"
        if name not in self.data and len(self.data) >= self.max_vars:
            raise VarError(f"too many variables ({self.max_vars}). Reuse or drop names first.")
        self.data[name] = text
        return text

    def get(self, name: str) -> str | None:
        return self.data.get(str(name or "").strip())

    def delete(self, name: str) -> bool:
        return self.data.pop(str(name or "").strip(), None) is not None

    def clear(self) -> None:
        self.data.clear()

    def names(self) -> list[str]:
        return sorted(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def __contains__(self, name) -> bool:
        return str(name) in self.data

    # ------------------------------------------------------------------ views
    def summary(self, width: int = 70) -> str:
        """Compact listing for the system prompt / a compaction summary."""
        if not self.data:
            return ""
        out = []
        for name in self.names():
            value = self.data[name]
            first = value.strip().splitlines()[0] if value.strip() else ""
            if len(first) > width:
                first = first[: width - 1] + "…"
            lines = len(value.splitlines())
            meta = f"{len(value)} chars" + (f", {lines} lines" if lines > 1 else "")
            out.append(f"- {name} ({meta}): {first}")
        return "\n".join(out)

    # ------------------------------------------------------------- expansion
    def expand(self, text: str) -> str:
        """Replace {{name}} with the value. Unknown names are left untouched."""
        if not isinstance(text, str) or "{{" not in text:
            return text
        return PLACEHOLDER_RE.sub(
            lambda m: self.data.get(m.group(1), m.group(0)), text)

    def expand_whole(self, text: str) -> str:
        """Replace only a value that is exactly one placeholder, e.g. '{{plan}}'.

        Used for file contents, so that templates containing {{...}} survive.
        """
        if not isinstance(text, str) or "{{" not in text:
            return text
        m = WHOLE_RE.match(text)
        if m and m.group(1) in self.data:
            return self.data[m.group(1)]
        return text

    def expand_args(self, args: dict, fields: tuple | None = None,
                    whole_fields: tuple = ()) -> dict:
        """Expand placeholders in tool arguments.

        fields=None expands every string (recursively, inside lists/dicts).
        Keys listed in whole_fields are only expanded when the whole value is
        a single placeholder.
        """
        def walk(value):
            if isinstance(value, str):
                return self.expand(value)
            if isinstance(value, list):
                return [walk(v) for v in value]
            if isinstance(value, dict):
                return {k: walk(v) for k, v in value.items()}
            return value

        out = {}
        for key, value in args.items():
            if key in whole_fields:
                out[key] = self.expand_whole(value) if isinstance(value, str) else walk(value)
            elif fields is None or key in fields:
                out[key] = walk(value)
            else:
                out[key] = value
        return out

    # ------------------------------------------------------------ (de)serialize
    def to_dict(self) -> dict[str, str]:
        return dict(self.data)

    def update(self, data: dict | None) -> None:
        for name, value in (data or {}).items():
            try:
                self.set(name, value)
            except VarError:
                continue
