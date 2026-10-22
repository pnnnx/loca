"""Индекс кода: кто кого вызывает, что функция принимает, отдаёт и бросает.

repo_map показывает, где что лежит; индекс отвечает на вопрос «что сломается, если это
поменять». Хранится в .loca/index.json и пересчитывается только для файлов с новым
mtime или размером. Связи строятся только для Python.

Инструменты deps и impact включаются флагом codeindex (по умолчанию выключен), чтобы
A/B-сравнение шло на одном и том же коде.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

from .base import Tool, ToolContext, ToolError, obj, truncate
from .codemap import _args_sig, _doc_line, _test_index
from .fs import _read_text, _walk

INDEX_VERSION = 1
PY_SUFFIXES = (".py", ".pyi")

# префикс полного имени вызова -> вид побочного эффекта
_EFFECTS = {
    "print": "вывод", "input": "ввод", "open": "файлы", "exit": "выход", "quit": "выход",
    "sys.exit": "выход", "sys.stdout": "вывод", "sys.stderr": "вывод",
    "subprocess": "процессы", "os.system": "процессы", "os.popen": "процессы", "os.kill": "процессы",
    "os.remove": "файлы", "os.unlink": "файлы", "os.rename": "файлы", "os.replace": "файлы",
    "os.makedirs": "файлы", "os.mkdir": "файлы", "os.rmdir": "файлы", "os.chmod": "файлы",
    "os.chdir": "ОС", "os.putenv": "ОС", "shutil": "файлы", "tempfile": "файлы",
    "requests": "сеть", "httpx": "сеть", "urllib.request": "сеть", "socket": "сеть",
    "http.client": "сеть", "time.sleep": "ожидание", "random": "случайность", "logging": "лог",
}
_FILE_METHODS = {"write", "writelines", "write_text", "write_bytes", "unlink", "mkdir", "rmdir",
                 "touch", "chmod", "rename"}
_MUTATORS = {"append", "extend", "insert", "pop", "popitem", "remove", "clear", "update",
             "setdefault", "add", "discard", "sort", "reverse"}
# аннотации, по которым не стоит угадывать тип получателя
_PLAIN_TYPES = {"str", "int", "float", "bool", "bytes", "list", "dict", "set", "tuple", "object",
                "Any", "Path"}


def _dotted(node) -> str | None:
    """foo / a.b.c / Foo().run / super.run / ?.run (получатель неизвестен)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = node.value
        if isinstance(base, ast.Call):
            if isinstance(base.func, ast.Name) and base.func.id == "super":
                return f"super.{node.attr}"
            inner = _dotted(base.func)
            if inner and "?" not in inner and "()" not in inner:
                return f"{inner}().{node.attr}"
            return f"?.{node.attr}"
        inner = _dotted(base)
        return f"{inner}.{node.attr}" if inner else f"?.{node.attr}"
    return None


def _params(node) -> list[dict]:
    a = node.args
    pos = a.posonlyargs + a.args
    first_default = len(pos) - len(a.defaults)
    out = [{"n": p.arg, "k": "posonly" if i < len(a.posonlyargs) else "pos", "d": i >= first_default}
           for i, p in enumerate(pos)]
    if a.vararg:
        out.append({"n": a.vararg.arg, "k": "var", "d": True})
    out += [{"n": p.arg, "k": "kw", "d": d is not None} for p, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        out.append({"n": a.kwarg.arg, "k": "varkw", "d": True})
    return out


def _ann_type(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
    name = _dotted(node)
    if not name or "?" in name or "()" in name or name.split(".")[-1] in _PLAIN_TYPES:
        return None
    return name


class _Scan(ast.NodeVisitor):
    """Вызовы, исключения, возвраты и побочные эффекты тела одной функции."""

    def __init__(self, imports: dict, params: list[str], types: dict):
        self.imports, self.params = imports, set(params) - {"self", "cls"}
        self.types = dict(types)          # переменная -> имя класса (по аннотации или x = Foo())
        self.calls, self.raises, self.effects, self.returns = [], [], [], []
        self._discarded: set[int] = set()
        self._depth = 0

    def _effect(self, text: str) -> None:
        if text not in self.effects:
            self.effects.append(text)

    def _full(self, name: str) -> str:
        head, _, rest = name.partition(".")
        target = self.imports.get(head)
        return (target + ("." + rest if rest else "")) if target else name

    def _nested(self, node):
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _nested

    def visit_Expr(self, node):
        call = node.value.value if isinstance(node.value, ast.Await) else node.value
        if isinstance(call, ast.Call):
            self._discarded.add(id(call))
        self.generic_visit(node)

    def visit_Call(self, node):
        name = _dotted(node.func)
        if name:
            parts = name.split(".")
            if len(parts) == 2 and parts[0] in self.types:
                raw = f"{self.types[parts[0]]}().{parts[1]}"
            else:
                raw = name
            star = any(isinstance(a, ast.Starred) for a in node.args)
            kws = [k.arg for k in node.keywords if k.arg] + (
                ["**"] if any(k.arg is None for k in node.keywords) else [])
            self.calls.append({"n": raw, "l": node.lineno, "u": id(node) not in self._discarded,
                               "a": -1 if star else len(node.args), "k": kws})
            full = self._full(name)
            for prefix, kind in _EFFECTS.items():
                if full == prefix or full.startswith(prefix + "."):
                    self._effect(f"{kind}: {name}")
                    break
            last = parts[-1]
            if len(parts) > 1 and last in _FILE_METHODS:
                self._effect(f"файлы: {name}")
            if last in _MUTATORS and len(parts) >= 2:
                if parts[0] in self.params and len(parts) == 2:
                    self._effect(f"меняет аргумент {parts[0]}")
                elif parts[0] == "self" and len(parts) >= 3:
                    self._effect(f"меняет self.{parts[1]}")
        self.generic_visit(node)

    def _target(self, t):
        if isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._target(e)
            return
        if not isinstance(t, (ast.Attribute, ast.Subscript)):
            return
        root, first_attr = t, None
        while isinstance(root, (ast.Attribute, ast.Subscript)):
            if isinstance(root, ast.Attribute):
                first_attr = root.attr
            root = root.value
        if not isinstance(root, ast.Name):
            return
        if root.id in ("self", "cls"):
            self._effect(f"меняет {root.id}.{first_attr}" if first_attr else f"меняет {root.id}")
        elif root.id in self.params:
            self._effect(f"меняет аргумент {root.id}")
        elif self._full(root.id) == "os" and "environ" in ast.unparse(t):
            self._effect("ОС: os.environ")

    def visit_Assign(self, node):
        for t in node.targets:
            self._target(t)
        if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)):
            cls = _dotted(node.value.func)
            if cls and "?" not in cls and "()" not in cls:
                self.types[node.targets[0].id] = cls
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._target(node.target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._target(node.target)
        self.generic_visit(node)

    def visit_Global(self, node):
        self._effect("global " + ", ".join(node.names))

    visit_Nonlocal = visit_Global

    def visit_Raise(self, node):
        exc = node.exc
        name = "(перебрасывает)" if exc is None else _dotted(exc.func if isinstance(exc, ast.Call) else exc)
        if name and name not in self.raises:
            self.raises.append(name)
        self.generic_visit(node)

    def visit_Return(self, node):
        if self._depth == 0:
            none = node.value is None or (isinstance(node.value, ast.Constant) and node.value.value is None)
            kind = "None" if none else "значение"
            if kind not in self.returns:
                self.returns.append(kind)
        self.generic_visit(node)

    def visit_Yield(self, node):
        if self._depth == 0 and "генератор" not in self.returns:
            self.returns.insert(0, "генератор")
        self.generic_visit(node)

    visit_YieldFrom = visit_Yield


def _module_name(rel: str) -> str:
    parts = rel.rsplit(".", 1)[0].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(tree, module: str, is_pkg: bool) -> dict:
    """локальное имя -> полное имя; включая импорты внутри функций (так пишут тесты)."""
    pkg = module if is_pkg else module.rpartition(".")[0]
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    head = a.name.split(".")[0]
                    out[head] = head
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".") if pkg else []
                if node.level > 1:
                    base = base[:max(0, len(base) - (node.level - 1))]
                base = ".".join(base + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
    return out


def _function(node, imports: dict, cls: str | None) -> dict:
    params = _params(node)
    types = {}
    for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
        t = _ann_type(a.annotation) if a.annotation is not None else None
        if t:
            types[a.arg] = t
    scan = _Scan(imports, [p["n"] for p in params], types)
    for stmt in node.body:
        scan.visit(stmt)
    sig = _args_sig(node)
    ret = None
    if node.returns is not None:
        try:
            ret = ast.unparse(node.returns)
        except Exception:  # noqa: BLE001
            ret = None
        sig = sig.rsplit(" -> ", 1)[0]
    return {"kind": "async def" if isinstance(node, ast.AsyncFunctionDef) else "def",
            "line": node.lineno, "end": node.end_lineno or node.lineno, "cls": cls,
            "sig": sig, "params": params, "ret": ret, "returns": scan.returns or ["None"],
            "raises": scan.raises, "effects": scan.effects[:8], "calls": scan.calls,
            "doc": _doc_line(node)}


def scan_file(rel: str, text: str, mtime: int = 0, size: int = 0) -> dict:
    module = _module_name(rel)
    rec = {"mtime": mtime, "size": size, "module": module, "imports": {}, "symbols": {}}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        rec["error"] = "SyntaxError"
        return rec
    imports = _imports(tree, module, rel.endswith("__init__.py"))
    rec["imports"] = imports
    syms = rec["symbols"]
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    top = _Scan(imports, [], {})
    for node in tree.body:
        if isinstance(node, defs):
            syms[node.name] = _function(node, imports, None)
        elif isinstance(node, ast.ClassDef):
            bases = [b for b in (_dotted(x) for x in node.bases) if b and "?" not in b]
            syms[node.name] = {"kind": "class", "line": node.lineno,
                               "end": node.end_lineno or node.lineno, "bases": bases,
                               "sig": f"({', '.join(bases)})" if bases else "",
                               "doc": _doc_line(node), "calls": []}
            for sub in node.body:
                if isinstance(sub, defs):
                    syms[f"{node.name}.{sub.name}"] = _function(sub, imports, node.name)
        elif not isinstance(node, (ast.Import, ast.ImportFrom)):
            top.visit(node)
    if top.calls:
        syms["<module>"] = {"kind": "module", "line": 1, "end": 1, "calls": top.calls,
                            "effects": top.effects[:8]}
    return rec


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def _ignore_in_git(folder: Path, name: str) -> None:
    """Кэш индекса — не для коммита: строка в .loca/.gitignore (config.json и commands/ остаются в git)."""
    gi = folder / ".gitignore"
    line = name + "*"                         # index.json и index.json.tmp
    try:
        if not gi.exists():                   # свой файл прячет и себя: в git status пусто
            gi.write_text(f"{line}\n.gitignore\n", encoding="utf-8")
            return
        text = gi.read_text(encoding="utf-8")
        if line not in text.splitlines():
            gi.write_text(text + ("" if not text or text.endswith("\n") else "\n") + line + "\n",
                          encoding="utf-8")
    except OSError:
        pass


class CodeIndex:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = self.root / ".loca" / "index.json"
        self.files: dict[str, dict] = {}
        self.reparsed: list[str] = []
        self._graph = None

    # ------------------------------------------------------------ хранение
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return {}
        return data.get("files") or {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _ignore_in_git(self.path.parent, self.path.name)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"version": INDEX_VERSION, "files": self.files},
                                      ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass                      # индекс — кэш: без записи просто посчитаем заново

    def refresh(self) -> "CodeIndex":
        old = self.files or self._load()
        files, reparsed = {}, []
        for p in _walk(self.root):
            if p.suffix not in PY_SUFFIXES:
                continue
            rel = p.relative_to(self.root).as_posix()
            try:
                st = p.stat()
            except OSError:
                continue
            hit = old.get(rel)
            if hit and hit.get("mtime") == st.st_mtime_ns and hit.get("size") == st.st_size:
                files[rel] = hit
                continue
            try:
                text = _read_text(p)
            except OSError:
                continue
            files[rel] = scan_file(rel, text, st.st_mtime_ns, st.st_size)
            reparsed.append(rel)
        changed = bool(reparsed) or set(files) != set(old)
        self.files, self.reparsed = files, reparsed
        if changed or not self.path.exists():
            self._save()
        if changed or self._graph is None:
            self._build()
        return self

    # ------------------------------------------------------------ разрешение имён
    def _build(self) -> None:
        self.symbols: dict[str, tuple[str, str, dict]] = {}
        self.modules: dict[str, str] = {}
        for rel, f in self.files.items():
            mod = f["module"]
            self.modules[mod] = rel
            for local, rec in f["symbols"].items():
                self.symbols[self._q(mod, local)] = (rel, local, rec)
        self._mod_cache: dict[str, str | None] = {}
        self._res_cache: dict[tuple, str | None] = {}
        callers: dict[str, list] = {}
        maybe: dict[str, list] = {}
        callees: dict[str, list] = {}
        for q, (rel, local, rec) in self.symbols.items():
            for c in rec.get("calls", []):
                t = self.resolve_in(rel, c["n"], rec.get("cls"))
                if not t:
                    continue
                if t.startswith("?."):
                    maybe.setdefault(t[2:], []).append((q, rel, c))
                else:
                    callers.setdefault(t, []).append((q, rel, c))
                    if t not in callees.setdefault(q, []):
                        callees[q].append(t)
        self._graph = (callers, maybe, callees)

    @staticmethod
    def _q(mod: str, local: str) -> str:
        return f"{mod}.{local}" if mod else local

    def _find_module(self, name: str) -> str | None:
        if name in self.modules:
            return name
        if name not in self._mod_cache:
            cands = [m for m in self.modules if m.endswith("." + name)]
            self._mod_cache[name] = cands[0] if len(cands) == 1 else None
        return self._mod_cache[name]

    def canon(self, dotted: str, depth: int = 0) -> str | None:
        """Полное имя -> символ индекса, с переходом по реэкспортам (from .core import x)."""
        if dotted in self.symbols:
            return dotted
        if depth > 6:
            return None
        parts = dotted.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mod = self._find_module(".".join(parts[:i]))
            if mod is None:
                continue
            rest = parts[i:]
            q = self._q(mod, ".".join(rest))
            if q in self.symbols:
                return q
            target = self.files[self.modules[mod]]["imports"].get(rest[0])
            if target:
                return self.canon(".".join([target] + rest[1:]), depth + 1)
            if len(rest) == 2 and self._q(mod, rest[0]) in self.symbols:
                return self.method(self._q(mod, rest[0]), rest[1], depth + 1)
            return None
        return None

    def method(self, cls_q: str, name: str, depth: int = 0) -> str | None:
        q = f"{cls_q}.{name}"
        if q in self.symbols:
            return q
        hit = self.symbols.get(cls_q)
        if not hit or hit[2].get("kind") != "class" or depth > 6:
            return None
        rel = hit[0]
        for base in hit[2].get("bases", []):
            bq = self.resolve_in(rel, base, None)
            if bq and not bq.startswith("?."):
                found = self.method(bq, name, depth + 1)
                if found:
                    return found
        return None

    def resolve_in(self, rel: str, raw: str, cls: str | None) -> str | None:
        """Имя вызова в файле rel -> символ; '?.имя', если получатель неизвестен; None — внешнее."""
        key = (rel, raw, cls)
        if key in self._res_cache:
            return self._res_cache[key]
        self._res_cache[key] = None           # защита от циклов
        result = self._resolve(rel, raw, cls)
        self._res_cache[key] = result
        return result

    def _resolve(self, rel: str, raw: str, cls: str | None) -> str | None:
        f = self.files[rel]
        mod = f["module"]
        if "()." in raw:
            pre, _, attr = raw.partition("().")
            if "." in attr:
                return "?." + attr.split(".")[-1]
            cq = self.resolve_in(rel, pre, cls)
            if cq and not cq.startswith("?.") and self.symbols[cq][2].get("kind") == "class":
                return self.method(cq, attr) or "?." + attr
            return "?." + attr if cq else None
        parts = raw.split(".")
        head = parts[0]
        if head == "?":
            return raw
        if head in ("self", "cls") and cls:
            if len(parts) == 2:
                return self.method(self._q(mod, cls), parts[1]) or "?." + parts[1]
            return "?." + parts[-1]
        if head == "super" and cls and len(parts) == 2:
            for base in self.symbols.get(self._q(mod, cls), ("", "", {}))[2].get("bases", []):
                bq = self.resolve_in(rel, base, None)
                if bq and not bq.startswith("?."):
                    found = self.method(bq, parts[1])
                    if found:
                        return found
            return "?." + parts[1]
        if head in f["symbols"] and f["symbols"][head].get("kind") != "module":
            return self.canon(self._q(mod, raw)) or ("?." + parts[-1] if len(parts) > 1 else None)
        if head in f["imports"]:
            return self.canon(".".join([f["imports"][head]] + parts[1:]))
        return "?." + parts[-1] if len(parts) > 1 else None

    # ------------------------------------------------------------ запросы
    def callers(self, q: str) -> list:
        out = list(self._graph[0].get(q, []))
        if q.endswith(".__init__"):
            out += self._graph[0].get(q[: -len(".__init__")], [])
        return out

    def maybe_callers(self, name: str) -> list:
        return self._graph[1].get(name, [])

    def callees(self, q: str) -> list:
        return self._graph[2].get(q, [])

    def find(self, sym: str) -> list[str]:
        file_part = None
        if ":" in sym:
            file_part, sym = (s.strip() for s in sym.split(":", 1))
        sym = sym.strip().rstrip("()")
        if not file_part and sym in self.symbols:
            return [sym]
        out = []
        for q, (rel, local, rec) in self.symbols.items():
            if rec.get("kind") == "module":
                continue
            if file_part and rel != file_part and not rel.endswith("/" + file_part):
                continue
            if q == sym or local == sym or q.endswith("." + sym):
                out.append(q)
        return sorted(out, key=lambda q: (_is_test_file(self.symbols[q][0]), q))

    def tests_for(self, qs) -> list[str]:
        """Тестовые функции, которые вызывают символы qs (точно или по имени метода)."""
        out = []
        for q in qs:
            name = q.rsplit(".", 1)[-1]
            for cq, crel, _ in self.callers(q) + self.maybe_callers(name):
                if _is_test_file(crel) and cq.rsplit(".", 1)[-1].startswith("test"):
                    label = f"{crel}::{self.symbols[cq][1].replace('.', '::')}"
                    if label not in out:
                        out.append(label)
        return out

    def file_tests(self, rel: str) -> list[str]:
        stem = rel.rsplit("/", 1)[-1].split(".")[0]
        index = _test_index([Path(r) for r in self.files if _is_test_file(r)])
        return [p.as_posix() for p in index.get(stem, [])]

    def _where(self, item) -> str:
        cq, crel, c = item
        local = self.symbols[cq][1]
        return f"{crel}:{c['l']} " + ("(уровень модуля)" if local == "<module>" else local)

    def describe(self, q: str) -> str:
        rel, local, rec = self.symbols[q]
        f = self.files[rel]
        out = [f"{q}  ({rel}:{rec['line']}-{rec['end']})"]
        if rec.get("doc"):
            out.append(f"  {rec['doc']}")
        if rec["kind"] == "class":
            out.append(f"класс{rec.get('sig', '')}")
            methods = [(lq, r) for lq, r in f["symbols"].items() if r.get("cls") == local]
            if methods:
                out.append("методы: " + ", ".join(f"{lq.split('.', 1)[1]}{r['sig']}" for lq, r in methods[:15]))
        else:
            out.append(f"принимает: {rec['sig']}")
            inferred = " или ".join(rec.get("returns") or ["None"])
            out.append("возвращает: " + (f"{rec['ret']} (по коду: {inferred})" if rec.get("ret")
                                         else f"не указано, по коду: {inferred}"))
            out.append("исключения: " + (", ".join(rec.get("raises") or []) or "—"))
            out.append("побочные эффекты: " + (", ".join(rec.get("effects") or []) or "—"))
            calls = self.callees(q)
            if calls:
                more = f" … ещё {len(calls) - 15}" if len(calls) > 15 else ""
                out.append(f"вызывает ({len(calls)}): " + ", ".join(calls[:15]) + more)
        callers = self.callers(q)
        seen = set()
        uniq = [c for c in callers if (c[0], c[2]["l"]) not in seen and not seen.add((c[0], c[2]["l"]))]
        if uniq:
            more = f"; … ещё {len(uniq) - 15}" if len(uniq) > 15 else ""
            out.append(f"вызывают ({len(uniq)}): " + "; ".join(self._where(c) for c in uniq[:15]) + more)
        else:
            out.append("вызывают: никто (по индексу)")
        name = local.rsplit(".", 1)[-1]
        if rec["kind"] != "class" and "." in local:
            maybe = [c for c in self.maybe_callers(name) if (c[0], c[2]["l"]) not in seen]
            if maybe:
                out.append(f"возможно вызывают (.{name}, получатель неизвестен, {len(maybe)}): "
                           + "; ".join(self._where(c) for c in maybe[:5])
                           + (" …" if len(maybe) > 5 else ""))
        tests = self.tests_for([q])
        own = self.file_tests(rel)
        if tests or own:
            line = "тесты: " + (", ".join(tests[:10]) if tests else "прямых вызовов нет")
            if own:
                line += f" · тесты файла: {', '.join(own[:2])}"
            out.append(line)
        else:
            out.append("тесты: нет")
        imports = sorted(set(f["imports"].values()))
        if imports:
            out.append(f"импорты файла ({len(imports)}): " + ", ".join(imports[:14])
                       + (" …" if len(imports) > 14 else ""))
        return "\n".join(out)

    def impact(self, rels: list[str], depth: int = 2) -> dict:
        start = [q for q, (r, _, _) in self.symbols.items() if r in rels]
        seen, frontier, levels = set(start), start, []
        for _ in range(depth):
            nxt = []
            for q in frontier:
                for cq, _, _ in self.callers(q):
                    if cq not in seen:
                        seen.add(cq)
                        nxt.append(cq)
            if not nxt:
                break
            levels.append(nxt)
            frontier = nxt
        mods = {self.files[r]["module"] for r in rels}
        importers = []
        for rel, f in self.files.items():
            if rel in rels:
                continue
            for target in f["imports"].values():
                parts = target.split(".")
                hit = any(self._find_module(".".join(parts[:i])) in mods for i in range(len(parts), 0, -1))
                if hit:
                    importers.append(rel)
                    break
        tests = []
        for q in seen:
            rel, local, _ = self.symbols[q]
            if _is_test_file(rel) and local.rsplit(".", 1)[-1].startswith("test"):
                tests.append(f"{rel}::{local.replace('.', '::')}")
        test_files = sorted({t.split("::")[0] for t in tests}
                            | {r for r in importers if _is_test_file(r)}
                            | {t for r in rels for t in self.file_tests(r)})
        return {"start": start, "levels": levels, "importers": importers,
                "tests": sorted(tests), "test_files": test_files}


_INDEXES: dict[str, CodeIndex] = {}


def load_index(ctx: ToolContext) -> CodeIndex:
    root = str(Path(ctx.cwd).resolve())
    idx = _INDEXES.get(root)
    if idx is None:
        idx = _INDEXES[root] = CodeIndex(Path(root))
    return idx.refresh()


def _changed_files(cwd: Path) -> list[str]:
    out = []
    # --relative: пути от cwd, как у ls-files; без него из подпапки пути шли бы от корня репозитория
    for cmd in (["git", "diff", "--name-only", "--relative", "HEAD"],
                ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        if res.returncode != 0:
            return []
        out += [line.strip() for line in res.stdout.splitlines() if line.strip()]
    return out


class Deps(Tool):
    name = "deps"
    description = ("Связи одной функции, метода или класса по индексу кода: что принимает и "
                   "возвращает, какие исключения бросает, побочные эффекты (файлы, сеть, процессы, "
                   "изменение аргументов), кого вызывает, кто вызывает её, импорты файла и тесты. "
                   "Вызывайте перед изменением функции: видно, кого заденет правка. Только Python.")
    parameters = obj({
        "symbol": {"type": "string",
                   "description": "Имя: 'total', 'Cart.add', 'pkg.core.total' или 'pkg/core.py:Cart.add'"},
    }, ["symbol"])

    def summary(self, args):
        return str(args.get("symbol") or "")

    def run(self, args, ctx: ToolContext):
        sym = str(args.get("symbol") or "").strip()
        if not sym:
            raise ToolError("Укажите symbol, например 'Cart.add'.")
        idx = load_index(ctx)
        found = idx.find(sym)
        if not found:
            raise ToolError(f"Символ «{sym}» не найден в индексе (только Python). "
                            "repo_map покажет, какие символы есть.")
        if len(found) > 3:
            lines = [f"Под «{sym}» подходят {len(found)} символов, уточните (файл:имя или Класс.метод):"]
            lines += [f"  {q}  ({idx.symbols[q][0]}:{idx.symbols[q][2]['line']})" for q in found[:20]]
            return "\n".join(lines)
        return truncate("\n\n".join(idx.describe(q) for q in found), ctx.cfg["max_tool_output"])


class Impact(Tool):
    name = "impact"
    description = ("Что заденет изменение файлов: функции в других файлах, которые их вызывают "
                   "(по уровням), модули, которые их импортируют, и тесты, которые стоит запустить. "
                   "Вызывайте после правки, перед run_tests. Без files — файлы, изменённые в git. Только Python.")
    parameters = obj({
        "files": {"type": "array", "items": {"type": "string"}, "description": "Пути изменённых файлов"},
        "depth": {"type": "integer", "description": "Глубина по вызовам, 1–4 (по умолчанию 2)"},
    }, [])

    def summary(self, args):
        files = args.get("files") or ""
        return " ".join(files) if isinstance(files, list) else str(files)

    def run(self, args, ctx: ToolContext):
        raw = args.get("files") or []
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        from_git = not raw
        if from_git:
            raw = _changed_files(ctx.cwd)
            if not raw:
                raise ToolError("Изменённых файлов в git нет (или это не git). Передайте files.")
        depth = max(1, min(int(args.get("depth") or 2), 4))
        idx = load_index(ctx)
        rels, outside = [], []
        for item in raw:
            try:
                rel = ctx.resolve(str(item)).resolve().relative_to(idx.root).as_posix()
            except (ValueError, OSError):
                rel = str(item)
            (rels if rel in idx.files else outside).append(rel)
        if not rels:
            raise ToolError("Ни одного из файлов нет в индексе (индексируется только Python): "
                            + ", ".join(outside[:10]))
        res = idx.impact(rels, depth)
        counts = {r: sum(1 for q in res["start"] if idx.symbols[q][0] == r) for r in rels}
        lines = [("изменены (git): " if from_git else "изменены: ")
                 + ", ".join(f"{r} ({counts[r]} симв.)" for r in rels)]
        if outside:
            lines.append("вне индекса: " + ", ".join(outside[:10]))
        if not res["levels"]:
            lines.append("вызывающих в других местах нет")
        for n, level in enumerate(res["levels"], 1):
            by_file: dict[str, list[str]] = {}
            for q in level:
                rel, local, _ = idx.symbols[q]
                by_file.setdefault(rel, []).append("(уровень модуля)" if local == "<module>" else local)
            title = "вызывают изменённое" if n == 1 else f"вызывают уровень {n - 1}"
            lines.append(f"уровень {n} — {title} ({len(level)} в {len(by_file)} файлах):")
            for rel in sorted(by_file)[:20]:
                names = by_file[rel]
                lines.append(f"  {rel}: " + ", ".join(names[:12]) + (f", … +{len(names) - 12}" if len(names) > 12 else ""))
        if res["importers"]:
            imp = res["importers"]
            lines.append(f"импортируют ({len(imp)}): " + ", ".join(imp[:15]) + (" …" if len(imp) > 15 else ""))
        if res["tests"] or res["test_files"]:
            names = [t.split("::")[-1] for t in res["tests"]]
            lines.append(f"тесты ({len(res['tests'])}): " + (", ".join(res["tests"][:12]) or "прямых нет")
                         + (" …" if len(res["tests"]) > 12 else ""))
            if res["test_files"]:
                cmd = "python3 -m pytest " + " ".join(res["test_files"][:5])
                if names and len(names) <= 15:
                    cmd += ' -k "' + " or ".join(dict.fromkeys(names)) + '"'
                lines.append(f"запуск: {cmd}")
        else:
            lines.append("тестов, которые это проверяют, не найдено")
        return truncate("\n".join(lines), ctx.cfg["max_tool_output"])
