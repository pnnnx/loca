"""File-system tools: read, write, edit, list, glob, grep."""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..ui import render_diff
from .base import Tool, ToolContext, ToolError, obj

IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
                ".pytest_cache", "dist", "build", ".next", ".idea", ".DS_Store", "target"}
MAX_LINE = 500


def _is_binary(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return False


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _describe(p: Path) -> str:
    try:
        lines = len(_read_text(p).splitlines())
        return f"{lines} lines, {p.stat().st_size} bytes"
    except OSError:
        return "unreadable"


def _check_fresh(ctx: ToolContext, p: Path, tool: str = "edit_file") -> None:
    key = str(p)
    rel = ctx.rel(p)
    if key not in ctx.read_files:
        extra = (" If you meant to create a new file, pick a path that does not exist yet."
                 if tool == "write_file" else "")
        raise ToolError(
            f"'{rel}' ALREADY EXISTS ({_describe(p)}) and you have not read it in this "
            f"conversation. It exists — do not assume otherwise. Next call: "
            f"read_file with path '{rel}'. Then {tool} will work." + extra
            + (" To replace it wholesale without reading, call write_file again with "
               "overwrite=true." if tool == "write_file" else ""))
    if p.stat().st_mtime != ctx.read_files[key]:
        raise ToolError(f"'{rel}' changed on disk since you read it ({_describe(p)}). "
                        f"Next call: read_file with path '{rel}', then repeat the change.")


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            yield Path(dirpath) / name


class ReadFile(Tool):
    name = "read_file"
    description = ("Read a text file. Returns numbered lines. Use offset/limit for big files. "
                   "Always read a file before editing it.")
    parameters = obj({
        "path": {"type": "string", "description": "File path (relative to project root or absolute)"},
        "offset": {"type": "integer", "description": "1-based line to start from (default 1)"},
        "limit": {"type": "integer", "description": "Max lines to return (default 400)"},
    }, ["path"])

    def run(self, args, ctx):
        p = ctx.resolve(args["path"])
        if not p.exists():
            siblings = difflib.get_close_matches(p.name, [x.name for x in p.parent.glob("*")]
                                                 if p.parent.exists() else [], n=3)
            hint = f" Did you mean: {', '.join(siblings)}?" if siblings else ""
            raise ToolError(f"File not found: {ctx.rel(p)}.{hint}")
        if p.is_dir():
            raise ToolError(f"{ctx.rel(p)} is a directory; use list_dir.")
        if _is_binary(p):
            return f"{ctx.rel(p)} is a binary file ({p.stat().st_size} bytes)."
        lines = _read_text(p).splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, int(args.get("limit") or 400))
        chunk = lines[offset - 1: offset - 1 + limit]
        ctx.read_files[str(p)] = p.stat().st_mtime
        if not lines:
            return f"{ctx.rel(p)} is empty."
        width = len(str(offset + len(chunk)))
        out = [f"{str(i).rjust(width)}\t{line[:MAX_LINE]}" for i, line in enumerate(chunk, offset)]
        end = offset + len(chunk) - 1
        if end < len(lines):
            out.append(f"… ({len(lines)} lines total; continue with offset={end + 1})")
        return "\n".join(out)


class WriteFile(Tool):
    name = "write_file"
    description = ("Create a new file, or fully overwrite an existing one. Prefer edit_file for "
                   "small changes. An existing file must be read first (or pass overwrite=true "
                   "when its current content does not matter).")
    parameters = obj({
        "path": {"type": "string"},
        "content": {"type": "string", "description": "Full file content"},
        "overwrite": {"type": "boolean",
                      "description": "Replace an existing file without reading it first. "
                                     "Only when its current content truly does not matter."},
    }, ["path", "content"])
    read_only = False
    kind = "edit"
    expand_vars = ("path",)
    expand_vars_whole = ("content",)

    def summary(self, args):
        return f"{args.get('path')}, {len(str(args.get('content', '')).splitlines())} lines"

    def preview(self, args, ctx):
        p = ctx.resolve(args["path"])
        old = _read_text(p) if p.exists() else ""
        return render_diff(old, args.get("content", ""), ctx.rel(p))

    def run(self, args, ctx):
        p = ctx.resolve(args["path"])
        if p.is_dir():
            raise ToolError(f"{ctx.rel(p)} is a directory, not a file.")
        existed = p.exists()
        if existed and not args.get("overwrite"):
            _check_fresh(ctx, p, tool="write_file")
        p.parent.mkdir(parents=True, exist_ok=True)
        content = args["content"]
        p.write_text(content, encoding="utf-8")
        ctx.read_files[str(p)] = p.stat().st_mtime
        what = "Overwrote" if existed else "Created"
        return f"{what} {ctx.rel(p)} ({len(content.splitlines())} lines)"


def _find_fuzzy(text: str, old: str) -> tuple[int, int, str] | None:
    """Match ignoring indentation/trailing whitespace. Returns (start_char, end_char, file_indent)."""
    file_lines = text.splitlines(keepends=True)
    old_lines = [l.strip() for l in old.strip("\n").splitlines()]
    if not old_lines:
        return None
    n = len(old_lines)
    hits = []
    for i in range(len(file_lines) - n + 1):
        if all(file_lines[i + j].strip() == old_lines[j] for j in range(n)):
            hits.append(i)
    if len(hits) != 1:
        return None
    i = hits[0]
    start = sum(len(l) for l in file_lines[:i])
    block = "".join(file_lines[i:i + n])
    trailing = 2 if block.endswith("\r\n") else (1 if block.endswith("\n") else 0)
    end = start + len(block) - trailing
    indent = re.match(r"[ \t]*", file_lines[i]).group(0)
    return start, end, indent


def _reindent(new: str, old: str, target_indent: str) -> str:
    old_first = old.strip("\n").splitlines()[0]
    old_indent = re.match(r"[ \t]*", old_first).group(0)
    if old_indent == target_indent:
        return new.strip("\n")
    out = []
    for line in new.strip("\n").splitlines():
        if line.startswith(old_indent):
            line = line[len(old_indent):]
        out.append(target_indent + line if line else line)
    return "\n".join(out)


class EditFile(Tool):
    name = "edit_file"
    description = ("Replace an exact snippet in a file. old_string must match the file text "
                   "(copy it from read_file output WITHOUT the line numbers) and be unique, "
                   "unless replace_all is true. Include 2-3 surrounding lines to make it unique.")
    parameters = obj({
        "path": {"type": "string"},
        "old_string": {"type": "string", "description": "Exact text to replace"},
        "new_string": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
    }, ["path", "old_string", "new_string"])
    read_only = False
    kind = "edit"
    expand_vars = ("path",)
    expand_vars_whole = ("old_string", "new_string")

    def summary(self, args):
        return str(args.get("path"))

    def _apply(self, text: str, args: dict) -> str:
        old, new = args["old_string"], args["new_string"]
        if old == new:
            raise ToolError("old_string and new_string are identical.")
        if old == "":
            if text.strip():
                raise ToolError("old_string is empty; use write_file to create files.")
            return new
        count = text.count(old)
        if count == 1 or (count > 1 and args.get("replace_all")):
            return text.replace(old, new)
        if count > 1:
            lines = [str(text[:m.start()].count("\n") + 1) for m in re.finditer(re.escape(old), text)]
            raise ToolError(f"old_string occurs {count} times (lines {', '.join(lines[:10])}). "
                            "Add more surrounding context or set replace_all=true.")
        fuzzy = _find_fuzzy(text, old)
        if fuzzy:
            start, end, indent = fuzzy
            return text[:start] + _reindent(new, old, indent) + text[end:]
        first = old.strip().splitlines()[0].strip() if old.strip() else ""
        candidates = difflib.get_close_matches(first, [l.strip() for l in text.splitlines()], n=3, cutoff=0.5)
        hint = ("\nClosest lines in file:\n" + "\n".join(candidates)) if candidates else ""
        raise ToolError("old_string not found in file. Re-read the file and copy the text exactly." + hint)

    def preview(self, args, ctx):
        p = ctx.resolve(args["path"])
        try:
            old = _read_text(p)
            return render_diff(old, self._apply(old, dict(args)), ctx.rel(p))
        except (ToolError, OSError):
            return None

    def run(self, args, ctx):
        p = ctx.resolve(args["path"])
        if not p.exists():
            if args.get("old_string", "") == "":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(args["new_string"], encoding="utf-8")
                ctx.read_files[str(p)] = p.stat().st_mtime
                return f"Created {ctx.rel(p)}"
            raise ToolError(f"File not found: {ctx.rel(p)}")
        _check_fresh(ctx, p, tool="edit_file")
        old = _read_text(p)
        new = self._apply(old, args)
        p.write_text(new, encoding="utf-8")
        ctx.read_files[str(p)] = p.stat().st_mtime
        added = len(new.splitlines()) - len(old.splitlines())
        return f"Edited {ctx.rel(p)} ({'+' if added >= 0 else ''}{added} lines)"


class ListDir(Tool):
    name = "list_dir"
    description = "List files and folders in a directory (non-recursive)."
    parameters = obj({"path": {"type": "string", "description": "Directory (default '.')"}}, [])

    def run(self, args, ctx):
        p = ctx.resolve(args.get("path") or ".")
        if not p.is_dir():
            raise ToolError(f"Not a directory: {ctx.rel(p)}")
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if child.name in IGNORED_DIRS:
                continue
            entries.append(child.name + "/" if child.is_dir() else f"{child.name}  ({child.stat().st_size} B)")
        return "\n".join(entries[:300]) or "(empty)"


class Glob(Tool):
    name = "glob"
    description = "Find files by glob pattern, e.g. '**/*.py' or 'src/**/test_*.ts'. Newest first."
    parameters = obj({
        "pattern": {"type": "string"},
        "path": {"type": "string", "description": "Base directory (default '.')"},
    }, ["pattern"])

    def run(self, args, ctx):
        base = ctx.resolve(args.get("path") or ".")
        pattern = args["pattern"]
        if not pattern.startswith("**/") and "/" not in pattern:
            pattern = "**/" + pattern
        matches = []
        for f in _walk(base):
            rel = str(f.relative_to(base))
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.replace("**/", "", 1)):
                matches.append(f)
            if len(matches) > 2000:
                break
        matches.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        if not matches:
            return "No files matched."
        out = [ctx.rel(f) for f in matches[:100]]
        if len(matches) > 100:
            out.append(f"… {len(matches) - 100} more")
        return "\n".join(out)


class Grep(Tool):
    name = "grep"
    description = "Search file contents with a regex. Returns file:line:text matches."
    parameters = obj({
        "pattern": {"type": "string", "description": "Regular expression"},
        "path": {"type": "string", "description": "File or directory (default '.')"},
        "glob": {"type": "string", "description": "Only files matching this glob, e.g. '*.py'"},
        "ignore_case": {"type": "boolean"},
    }, ["pattern"])

    def run(self, args, ctx):
        base = ctx.resolve(args.get("path") or ".")
        pattern = args["pattern"]
        limit = 100
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "--color=never", "-M", "300", pattern, str(base)]
            if args.get("ignore_case"):
                cmd.insert(1, "-i")
            if args.get("glob"):
                cmd[1:1] = ["-g", args["glob"]]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode == 2:
                raise ToolError(r.stderr.strip()[:500])
            lines = r.stdout.splitlines()
            prefix = str(ctx.cwd) + os.sep
            lines = [l[len(prefix):] if l.startswith(prefix) else l for l in lines]
        else:
            try:
                rx = re.compile(pattern, re.I if args.get("ignore_case") else 0)
            except re.error as e:
                raise ToolError(f"Bad regex: {e}")
            files = [base] if base.is_file() else _walk(base)
            lines = []
            for f in files:
                if args.get("glob") and not fnmatch.fnmatch(f.name, args["glob"]):
                    continue
                if _is_binary(f):
                    continue
                try:
                    for i, line in enumerate(_read_text(f).splitlines(), 1):
                        if rx.search(line):
                            lines.append(f"{ctx.rel(f)}:{i}:{line[:300]}")
                except OSError:
                    continue
                if len(lines) > limit * 3:
                    break
        if not lines:
            return "No matches."
        extra = f"\n… {len(lines) - limit} more matches, narrow the search" if len(lines) > limit else ""
        return "\n".join(lines[:limit]) + extra
