"""Карта репозитория и чтение по символу.

Маленькая модель обычно ошибается не в коде, а в поиске места: read_file на 400
строк съедает контекст, а нужно было 20 строк одной функции. repo_map даёт дерево
символов проекта, read_symbol — ровно одно определение.
"""
from __future__ import annotations

import ast
import copy
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


def _args_sig(node) -> str:
    """Аргументы без self/cls, с аннотациями и значениями по умолчанию, плюс -> возврат."""
    try:
        args = copy.copy(node.args)
        if args.posonlyargs and args.posonlyargs[0].arg in ("self", "cls"):
            args.posonlyargs = args.posonlyargs[1:]
        elif args.args and args.args[0].arg in ("self", "cls"):
            args.args = args.args[1:]
        sig = f"({ast.unparse(args)})"
        if node.returns is not None:
            sig += f" -> {ast.unparse(node.returns)}"
    except Exception:  # noqa: BLE001 — сигнатура не критична
        return "(…)"
    return sig if len(sig) <= 110 else sig[:107] + "…"


def _doc_line(node) -> str:
    doc = ast.get_docstring(node) or ""
    line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return line if len(line) <= 70 else line[:67] + "…"


def _python_outline(text: str) -> list[dict]:
    """[{line, kind, name, sig, doc, children:[{name, sig}]}] для верхнего уровня модуля.

    Сигнатура — «что принимает и что отдаёт»: по ней модель часто понимает функцию,
    не открывая её код (узкое место «длинный контекст»).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    out = []
    for node in tree.body:
        if isinstance(node, defs):
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            out.append({"line": node.lineno, "kind": kind, "name": node.name,
                        "sig": _args_sig(node), "doc": _doc_line(node), "children": []})
        elif isinstance(node, ast.ClassDef):
            methods = [{"name": n.name, "sig": _args_sig(n)} for n in node.body if isinstance(n, defs)]
            bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
            out.append({"line": node.lineno, "kind": "class", "name": node.name,
                        "sig": f"({bases})" if bases else "", "doc": _doc_line(node),
                        "children": methods})
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    out.append({"line": node.lineno, "kind": "const", "name": target.id,
                                "sig": "", "doc": "", "children": []})
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
                sig = re.sub(r"\s*\{\s*\}?\s*$", "", line.strip())
                sig = sig if len(sig) <= 110 else sig[:107] + "…"
                out.append({"line": i, "kind": kind, "name": name, "sig": sig, "doc": "",
                            "raw": True, "children": []})
                break
    return out


_CACHE: dict[str, tuple[int, int, int, list[dict]]] = {}   # путь -> (mtime_ns, size, строк, символы)
_CACHE_MAX = 4000


def _parse(path: Path, text: str) -> list[dict]:
    if path.suffix in (".py", ".pyi"):
        result = _python_outline(text)
        if result:
            return result
    return _regex_outline(text)


def outline(path: Path, text: str | None = None) -> list[dict]:
    if text is not None:
        return _parse(path, text)
    return cached_outline(path)[1]


def cached_outline(path: Path) -> tuple[int, list[dict]]:
    """(строк, символы) файла; повторный вызов для неизменённого файла не читает его заново."""
    st = path.stat()
    key = str(path)
    hit = _CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2], hit[3]
    text = _read_text(path)
    result = (len(text.splitlines()), _parse(path, text))
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (st.st_mtime_ns, st.st_size, *result)
    return result


def render_outline(rel: str, lines_total: int, symbols: list[dict], max_symbols: int = 25,
                   detail: bool = True, tests: str = "") -> str:
    """detail=True — с сигнатурами и первой строкой docstring; False — только имена."""
    head = f"{rel} ({lines_total} строк)" + (f" · тесты: {tests}" if tests else "")
    if not symbols:
        return head + "\n  (символов не найдено)"
    body = []
    for sym in symbols[:max_symbols]:
        if detail and sym.get("raw") and sym.get("sig"):
            line = f"  {sym['line']}: {sym['sig']}"     # не-Python: строка объявления целиком
        else:
            sig = sym.get("sig", "") if detail and sym["kind"] != "const" else ""
            line = f"  {sym['line']}: {sym['kind']} {sym['name']}{sig}"
        if detail and sym.get("doc"):
            line += f"  # {sym['doc']}"
        kids = [k if isinstance(k, dict) else {"name": k} for k in sym.get("children") or []]
        if kids and not detail:
            names = [k["name"] for k in kids]
            line += " — " + ", ".join(names[:12]) + (f", … +{len(names) - 12}" if len(names) > 12 else "")
        body.append(line)
        if kids and detail:
            body += [f"      .{k['name']}{k.get('sig', '')}" for k in kids[:12]]
            if len(kids) > 12:
                body.append(f"      … ещё {len(kids) - 12} методов")
    if len(symbols) > max_symbols:
        body.append(f"  … ещё {len(symbols) - max_symbols} символов")
    return "\n".join([head] + body)


_LANG_FAMILY = {".py": "py", ".pyi": "py", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
                ".ts": "js", ".tsx": "js"}


def _same_lang(a: str, b: str) -> bool:
    return _LANG_FAMILY.get(a, a) == _LANG_FAMILY.get(b, b)


def _test_index(files: list[Path]) -> dict[str, list[Path]]:
    """stem -> тестовые файлы (test_<stem>.py, <stem>_test.py, <stem>.test.js…)."""
    index: dict[str, list[Path]] = {}
    for f in files:
        n = f.name
        for stem in (n[5:].split(".")[0] if n.startswith("test_") else None,
                     n.split("_test.")[0] if "_test." in n else None,
                     n.split(".test.")[0] if ".test." in n else None,
                     n.split(".spec.")[0] if ".spec." in n else None):
            if stem:
                index.setdefault(stem, []).append(f)
    return index


class RepoMap(Tool):
    name = "repo_map"
    description = ("Карта кода: файлы и их символы (классы, функции, методы) с номерами строк, "
                   "сигнатурами (что принимают и что возвращают) и тестами файла. "
                   "Начинайте с неё вместо чтения файлов целиком: так видно, где что лежит, "
                   "за один вызов. path на файл даёт outline одного файла.")
    parameters = obj({
        "path": {"type": "string", "description": "Файл или каталог (по умолчанию '.')"},
        "pattern": {"type": "string", "description": "Фильтр по имени, например '*.py'"},
        "max_files": {"type": "integer", "description": "Сколько файлов показать (по умолчанию 60)"},
        "detail": {"type": "string", "enum": ["auto", "signatures", "names"],
                   "description": "auto (по умолчанию): сигнатуры, если карта помещается"},
    }, [])

    def summary(self, args):
        return str(args.get("path") or ".") + (f" {args['pattern']}" if args.get("pattern") else "")

    def run(self, args, ctx: ToolContext):
        base = ctx.resolve(args.get("path") or ".")
        if not base.exists():
            raise ToolError(f"Нет такого пути: {ctx.rel(base)}")
        pattern = (args.get("pattern") or "").strip()
        detail = str(args.get("detail") or "auto").lower()
        max_files = max(1, min(int(args.get("max_files") or 60), 400))
        project = [f for f in _walk(ctx.cwd) if f.suffix in CODE_SUFFIXES]
        files = [base] if base.is_file() else [
            f for f in (project if base == ctx.cwd.resolve() else _walk(base))
            if f.suffix in CODE_SUFFIXES and f.name not in IGNORED_DIRS]
        if pattern:
            import fnmatch
            files = [f for f in files if fnmatch.fnmatch(f.name, pattern)
                     or fnmatch.fnmatch(str(f), pattern)]
        if not files:
            raise ToolError("Файлов с кодом не нашлось. Попробуйте list_dir или другой pattern.")
        files = sorted(files, key=lambda f: str(f))
        tests = _test_index(project)
        shown, parsed, total_syms = files[:max_files], [], 0
        for f in shown:
            if _is_binary(f):
                continue
            try:
                n_lines, syms = cached_outline(f)
            except OSError:
                continue
            total_syms += len(syms)
            stem = f.name.split(".")[0]
            own = [] if stem.startswith("test_") else [
                t for t in tests.get(stem, []) if _same_lang(t.suffix, f.suffix)]
            parsed.append((ctx.rel(f), n_lines, syms, ", ".join(ctx.rel(t) for t in own[:2])))
        tail = ""
        if len(files) > len(shown):
            tail = f"\n… ещё {len(files) - len(shown)} файлов (уточните path или pattern)"
        limit = ctx.cfg["max_tool_output"]

        def build(with_sig: bool) -> str:
            return "\n".join(render_outline(r, n, sy, detail=with_sig, tests=t) for r, n, sy, t in parsed)

        body, note = build(detail != "names"), ""
        if detail == "auto" and len(body) > limit * 0.9:
            body = build(False)
            note = ("Сигнатуры скрыты — карта большая. repo_map с path на папку или файл "
                    "покажет, что функции принимают и возвращают.\n")
        header = (f"{len(shown)} файлов, {total_syms} символов. read_symbol покажет любой из них.\n"
                  + note)
        return truncate(header + body + tail, limit)


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
        if not args.get("path"):
            module = _module_file(name, ctx)
            if module is not None:
                path, rest = module
                if not rest:      # «app.cli» — это модуль, а не символ: показываем его карту
                    outline_text = RepoMap().run({"path": ctx.rel(path)}, ctx)
                    return (f"'{name}' — это модуль {ctx.rel(path)}, а не функция. Его состав:\n"
                            f"{outline_text}\nread_symbol с именем функции или класса покажет код.")
                args = {**args, "name": rest, "path": ctx.rel(path)}
                name = rest
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


def _module_file(dotted: str, ctx):
    """'pkg.core.calc_total' → (pkg/core.py, 'calc_total'); 'app.cli' → (app/cli.py, '').

    Модели часто зовут read_symbol с путём модуля в стиле import. Ищем самый длинный
    префикс, который является файлом или пакетом проекта; остаток — имя символа.
    """
    name = dotted.replace("/", ".")
    if name.endswith(".py"):
        name = name[:-3]
    parts = [p for p in name.split(".") if p]
    if len(parts) < 2 and not (ctx.cwd / f"{name}.py").is_file():
        return None
    for i in range(len(parts), 0, -1):
        base = ctx.cwd.joinpath(*parts[:i])
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                return candidate, ".".join(parts[i:])
    return None
