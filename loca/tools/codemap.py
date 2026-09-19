"""Карта репозитория и чтение по символу.

Маленькая модель обычно ошибается не в коде, а в поиске места: read_file на 400
строк съедает контекст, а нужно было 20 строк одной функции. repo_map даёт дерево
символов проекта, read_symbol — ровно одно определение.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from .base import Tool, ToolContext, ToolError, obj, truncate
from .fs import IGNORED_DIRS, _is_binary, _read_text, _walk

CODE_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs",
                 ".rb", ".sh", ".zsh", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp",
                 ".lua", ".php"}

# (регекс, номер группы с именем, вид) для языков без парсера в stdlib
_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"), 1, "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"), 1, "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"), 1, "function"),
    (re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), 1, "interface"),
    (re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*="), 1, "type"),
    (re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"), 1, "func"),
    (re.compile(r"^type\s+(\w+)\s"), 1, "type"),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"), 1, "fn"),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(\w+)"), 1, "type"),
    (re.compile(r"^\s*(?:def|module)\s+(\w+)"), 1, "def"),
    (re.compile(r"^(\w+)\s*\(\)\s*\{"), 1, "function"),
]


def _python_outline(text: str) -> list[dict]:
    """[{line, kind, name, children:[имена]}] для верхнего уровня модуля."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append({"line": node.lineno, "kind": "def", "name": node.name, "children": []})
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            out.append({"line": node.lineno, "kind": "class", "name": node.name,
                        "children": methods})
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    out.append({"line": node.lineno, "kind": "const", "name": target.id,
                                "children": []})
    return out


def _regex_outline(text: str) -> list[dict]:
    out, seen = [], set()
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 300:
            continue
        for rx, group, kind in _PATTERNS:
            m = rx.match(line)
            if m:
                name = m.group(group)
                if (name, kind) in seen:
                    break
                seen.add((name, kind))
                out.append({"line": i, "kind": kind, "name": name, "children": []})
                break
    return out


def outline(path: Path, text: str | None = None) -> list[dict]:
    if text is None:
        text = _read_text(path)
    if path.suffix in (".py", ".pyi"):
        result = _python_outline(text)
        if result:
            return result
    return _regex_outline(text)


def render_outline(rel: str, lines_total: int, symbols: list[dict], max_symbols: int = 25) -> str:
    head = f"{rel} ({lines_total} строк)"
    if not symbols:
        return head + "\n  (символов не найдено)"
    body = []
    for sym in symbols[:max_symbols]:
        line = f"  {sym['line']}: {sym['kind']} {sym['name']}"
        if sym.get("children"):
            kids = ", ".join(sym["children"][:12])
            if len(sym["children"]) > 12:
                kids += f", … +{len(sym['children']) - 12}"
            line += f" — {kids}"
        body.append(line)
    if len(symbols) > max_symbols:
        body.append(f"  … ещё {len(symbols) - max_symbols} символов")
    return "\n".join([head] + body)


class RepoMap(Tool):
    name = "repo_map"
    description = ("Карта кода: файлы и их символы (классы, функции, методы) с номерами строк. "
                   "Начинайте с неё вместо чтения файлов целиком: так видно, где что лежит, "
                   "за один вызов. path на файл даёт outline одного файла.")
    parameters = obj({
        "path": {"type": "string", "description": "Файл или каталог (по умолчанию '.')"},
        "pattern": {"type": "string", "description": "Фильтр по имени, например '*.py'"},
        "max_files": {"type": "integer", "description": "Сколько файлов показать (по умолчанию 60)"},
    }, [])

    def summary(self, args):
        return str(args.get("path") or ".") + (f" {args['pattern']}" if args.get("pattern") else "")

    def run(self, args, ctx: ToolContext):
        base = ctx.resolve(args.get("path") or ".")
        if not base.exists():
            raise ToolError(f"Нет такого пути: {ctx.rel(base)}")
        pattern = (args.get("pattern") or "").strip()
        max_files = max(1, min(int(args.get("max_files") or 60), 400))
        files = [base] if base.is_file() else [
            f for f in _walk(base)
            if f.suffix in CODE_SUFFIXES and f.name not in IGNORED_DIRS]
        if pattern:
            import fnmatch
            files = [f for f in files if fnmatch.fnmatch(f.name, pattern)
                     or fnmatch.fnmatch(str(f), pattern)]
        if not files:
            raise ToolError("Файлов с кодом не нашлось. Попробуйте list_dir или другой pattern.")
        files = sorted(files, key=lambda f: str(f))
        shown, blocks, total_syms = files[:max_files], [], 0
        for f in shown:
            if _is_binary(f):
                continue
            try:
                text = _read_text(f)
            except OSError:
                continue
            syms = outline(f, text)
            total_syms += len(syms)
            blocks.append(render_outline(ctx.rel(f), len(text.splitlines()), syms))
        tail = ""
        if len(files) > len(shown):
            tail = f"\n… ещё {len(files) - len(shown)} файлов (уточните path или pattern)"
        header = f"{len(shown)} файлов, {total_syms} символов. read_symbol покажет любой из них.\n"
        return truncate(header + "\n".join(blocks) + tail, ctx.cfg["max_tool_output"])


def _python_symbol(text: str, name: str) -> tuple[int, int] | None:
    """Ищет def/class по имени, в т.ч. 'Класс.метод'. Возвращает (первая, последняя) строка."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    owner, _, leaf = name.rpartition(".")
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)

    def span(node):
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        return start, getattr(node, "end_lineno", node.lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not owner and node.name == leaf:
            return span(node)
        if isinstance(node, ast.ClassDef) and owner in ("", node.name):
            for child in node.body:
                if isinstance(child, defs) and child.name == leaf:
                    return span(child)
        if isinstance(node, defs) and not owner and node.name == leaf:
            return span(node)
    return None


def _regex_symbol(text: str, name: str) -> tuple[int, int] | None:
    lines = text.splitlines()
    escaped = re.escape(name.rpartition(".")[2])
    start = None
    for i, line in enumerate(lines):
        for rx, group, _kind in _PATTERNS:
            m = rx.match(line)
            if m and m.group(group) == name.rpartition(".")[2]:
                start = i
                break
        if start is None and re.match(rf"^\s*(?:\w[\w \t*&:<>]*\s)?{escaped}\s*\(", line) \
                and line.rstrip().endswith("{"):
            start = i
        if start is not None:
            break
    if start is None:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    depth = lines[start].count("{") - lines[start].count("}")
    braces = "{" in lines[start]
    for j in range(start + 1, min(len(lines), start + 400)):
        line = lines[j]
        if braces:
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                end = j + 1
                break
        elif line.strip() and (len(line) - len(line.lstrip())) <= indent:
            end = j
            break
    else:
        end = min(len(lines), start + 400)
    return start + 1, end


class ReadSymbol(Tool):
    name = "read_symbol"
    description = ("Показывает одно определение целиком: функцию, класс или метод "
                   "('Класс.метод' тоже работает). Дешевле read_file, когда нужен конкретный "
                   "кусок. После него можно сразу править файл через edit_file.")
    parameters = obj({
        "name": {"type": "string", "description": "Имя символа, например run или Agent.execute"},
        "path": {"type": "string", "description": "Файл или каталог для поиска (по умолчанию '.')"},
    }, ["name"])

    def summary(self, args):
        return str(args.get("name"))

    def run(self, args, ctx: ToolContext):
        name = str(args["name"]).strip().strip("()")
        if not name:
            raise ToolError("name пустой")
        base = ctx.resolve(args.get("path") or ".")
        if not base.exists():
            raise ToolError(f"Нет такого пути: {ctx.rel(base)}")
        files = [base] if base.is_file() else [
            f for f in _walk(base) if f.suffix in CODE_SUFFIXES]
        hits = []
        for f in files:
            if _is_binary(f):
                continue
            try:
                text = _read_text(f)
            except OSError:
                continue
            if name.rpartition(".")[2] not in text:
                continue
            span = (_python_symbol(text, name) if f.suffix in (".py", ".pyi")
                    else None) or _regex_symbol(text, name)
            if span:
                hits.append((f, text, span))
            if len(hits) > 8:
                break
        if not hits:
            raise ToolError(f"Символ '{name}' не найден. Попробуйте repo_map или grep.")
        if len(hits) > 1:
            where = ", ".join(f"{ctx.rel(f)}:{s[0]}" for f, _t, s in hits[:8])
            raise ToolError(f"'{name}' найден в нескольких местах: {where}. "
                            "Повторите вызов с path нужного файла.")
        path, text, (start, end) = hits[0]
        lines = text.splitlines()[start - 1:end]
        try:
            ctx.read_files[str(path)] = path.stat().st_mtime   # чтобы edit_file не требовал read_file
        except OSError:
            pass
        width = len(str(end))
        body = "\n".join(f"{str(i).rjust(width)}\t{line[:500]}"
                         for i, line in enumerate(lines, start))
        return truncate(f"{ctx.rel(path)}:{start}-{end}\n{body}", ctx.cfg["max_tool_output"])
