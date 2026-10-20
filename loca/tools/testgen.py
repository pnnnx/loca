"""write_tests: тесты к Python-функции, где ожидаемые значения даёт запуск, а не модель.

Маленькая модель уверенно выдумывает результаты («6» вместо 14), поэтому она здесь только
придумывает ВХОДЫ. Дальше всё делает код:
  1. функция вызывается на каждом входе в отдельном процессе (дважды — ловим недетерминизм);
  2. настоящий результат или исключение становится assert / raises;
  3. тесты прогоняются (должны пройти) и проверяются мутациями: код функции по очереди чуть
     портится (< → <=, + → -, and → or, n → n+1, return x → return None), и если тесты порчу
     не ловят — это видно, и модель добавляет случаи.
Тесты фиксируют ТЕКУЩЕЕ поведение: подозрительное (IndexError на [], всё None) помечается.
"""
from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .base import Tool, ToolContext, ToolError, obj, truncate
from .tests import detect_test_command

SUSPICIOUS = {"IndexError", "ZeroDivisionError", "TypeError", "KeyError", "AttributeError",
              "UnboundLocalError", "NameError", "RecursionError"}
MAX_MUTANTS = 12
_SAFE_TOP = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
             ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass)
_HARNESS = r'''
import importlib, json, signal, sys
sys.path.insert(0, ".")
mod = importlib.import_module(sys.argv[1])
f = getattr(mod, sys.argv[2])
cases = json.loads(sys.argv[3])
def _alarm(*a): raise TimeoutError("timeout")
signal.signal(signal.SIGALRM, _alarm)
out = []
for case in cases:
    rec = {"case": case}
    signal.alarm(5)
    try:
        r = eval("__f(" + case + ")", {"__f": f})
        rec["ok"] = True
        rec["repr"] = repr(r)
        rec["type"] = type(r).__name__
        try:
            import ast as _a
            rec["literal"] = _a.literal_eval(repr(r)) == r
        except Exception:
            rec["literal"] = False
    except BaseException as e:
        rec["ok"] = False
        rec["exc"] = type(e).__name__
        rec["builtin_exc"] = type(e).__module__ == "builtins"
        rec["msg"] = str(e)[:120]
    finally:
        signal.alarm(0)
    out.append(rec)
print("@@JSON@@" + json.dumps(out))
'''


class WriteTests(Tool):
    name = "write_tests"
    description = ("Пишет тесты к Python-функции модуля по РЕАЛЬНОМУ запуску: вы даёте только входы "
                   "(cases), loca вызывает функцию и записывает настоящие результаты и исключения в "
                   "assert, прогоняет тесты и проверяет их мутациями. Ожидаемые значения не пишите.")
    parameters = obj({
        "target": {"type": "string", "description": "функция: путь/к/файлу.py:имя (или pkg.module.имя)"},
        "cases": {"type": "array", "items": {"type": "string"},
                  "description": "3–10 вызовов — аргументы как в Python: \"[1, 2, 3]\", \"[], tax=0.2\", "
                                 "\"'abc', 2\". Обычные случаи, границы (пусто, 0, отрицательные, None), ошибки"},
        "test_path": {"type": "string", "description": "куда писать (по умолчанию tests/test_<модуль>.py)"},
    }, ["target", "cases"])
    read_only = False
    kind = "edit"

    def summary(self, args):
        cases = args.get("cases") or []
        return f"{args.get('target')} · {len(cases) if isinstance(cases, list) else '?'} случаев"

    def permission_key(self, args):
        return f"write_tests({args.get('target', '')})"

    # ------------------------------------------------------------------ run
    def run(self, args, ctx: ToolContext):
        path, func = self._target(str(args["target"]), ctx)
        cases = _cases(args["cases"])
        if not cases:
            raise ToolError("cases пустой: дайте 3–10 вызовов, например [\"[1, 2, 3]\", \"[]\"]")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func), None)
        if fn is None:
            raise ToolError(f"в {ctx.rel(path)} нет функции верхнего уровня {func} "
                            "(методы классов пока не поддерживаются)")
        bad = [n for n in tree.body if (not isinstance(n, _SAFE_TOP) and not _main_guard(n))
               or (isinstance(n, ast.Expr) and not isinstance(n.value, ast.Constant))]
        if bad:
            raise ToolError(f"{ctx.rel(path)}: строка {bad[0].lineno} выполняется при импорте модуля — "
                            "запускать его автоматически нельзя. Уберите этот код в "
                            "if __name__ == '__main__': и вызовите снова.")
        from ..agent import _UNSAFE_RE
        m = _UNSAFE_RE.search(ast.get_source_segment(source, fn) or "")
        if m:
            raise ToolError(f"{func} делает `{m.group(0).strip()}` — сам не запускаю. Напишите тест вручную.")
        module = _module_name(path, ctx)

        first = _call(ctx, module, func, cases)
        second = _call(ctx, module, func, cases)
        unstable = {a["case"] for a, b in zip(first, second) if a != b}
        rows = [r for r in first if r["case"] not in unstable]
        if not rows:
            raise ToolError("результаты меняются от запуска к запуску (случайность/время) — "
                            "зафиксируйте seed или передайте зависимость параметром")

        framework = _framework(ctx)
        test_path = self._test_path(args.get("test_path"), path, ctx)
        code = _render(framework, module, func, rows)
        if ctx.snapshots is not None:
            ctx.snapshots.record(test_path, "write_tests")
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(code, encoding="utf-8")
        ctx.edited = True
        ctx.touched.add(str(test_path))
        ctx.read_files[str(test_path)] = test_path.stat().st_mtime

        run_cmd = _test_command(framework, test_path, ctx)
        ok, out = _run_tests(run_cmd, ctx)
        ctx.tests_run += 1
        ctx.tests_passed = ok
        report = [f"Записал {ctx.rel(test_path)}: {len(rows)} тестов к {module}.{func} ({framework}).",
                  "Ожидаемые значения получены ЗАПУСКОМ функции:"]
        for r in rows:
            got = r["repr"] if r["ok"] else f"исключение {r['exc']}: {r['msg']}"
            report.append(f"  {func}({r['case']}) → {got}")
        if unstable:
            report.append("Пропущено (результат меняется от запуска к запуску): " + ", ".join(sorted(unstable)))
        if not ok:
            report.append("⚠ Сгенерированные тесты не прошли — так быть не должно:\n" + truncate(out, 1200))
            return "\n".join(report)
        report.append(f"Тесты проходят: {run_cmd}")

        killed, survivors, total = _mutation_check(path, source, fn, run_cmd, ctx)
        if total:
            report.append(f"Мутационная проверка: тесты поймали {killed} из {total} порч кода функции.")
            for line, what in survivors[:6]:
                report.append(f"  ✗ не поймано: строка {line}: {what} — добавьте случай, где это меняет результат")
            if survivors:
                report.append("Добавьте случаи: снова write_tests с теми же и новыми cases. Бывает, что "
                              "порча не меняет поведение вовсе (например `<` → `<=` там, где на границе "
                              "обе ветки дают одно и то же) — такую не поймать, это нормально.")
        flags = [r for r in rows if not r["ok"] and r["exc"] in SUSPICIOUS]
        if all(r["ok"] and r["repr"] == "None" for r in rows):
            report.append("⚠ Функция всегда возвращает None — может, там print вместо return?")
        for r in flags[:4]:
            report.append(f"⚠ {func}({r['case']}) падает с {r['exc']} — это задумано? Если нет, это баг: "
                          "тест закрепил текущее (неправильное) поведение. Спросите пользователя.")
        report.append("Тесты фиксируют ТЕКУЩЕЕ поведение функции.")
        return "\n".join(report)

    # ------------------------------------------------------------------ helpers
    def _target(self, target: str, ctx) -> tuple[Path, str]:
        target = target.strip().replace("::", ":")
        if ":" in target:
            file, _, func = target.rpartition(":")
            path = ctx.resolve(file)
        else:
            from .codemap import _module_file
            found = _module_file(target, ctx)
            if not found or not found[1]:
                raise ToolError(f"не понял target '{target}': нужно путь/к/файлу.py:функция")
            path, func = found
        if "." in func:
            raise ToolError("методы классов пока не поддерживаются — только функции модуля")
        if not path.is_file() or path.suffix != ".py":
            raise ToolError(f"нет такого Python-файла: {ctx.rel(path)}")
        return path, func

    def _test_path(self, given, src: Path, ctx) -> Path:
        if given:
            return ctx.resolve(str(given))
        root = Path(ctx.cwd)
        base = next((root / d for d in ("tests", "test") if (root / d).is_dir()), root)
        p = base / f"test_{src.stem}.py"
        if p.exists() and "write_tests" not in p.read_text(encoding="utf-8", errors="replace")[:300]:
            p = base / f"test_{src.stem}_auto.py"          # чужой файл тестов не трогаем
        return p


def _cases(raw) -> list[str]:
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            raw = val if isinstance(val, list) else [raw]
        except json.JSONDecodeError:
            raw = [line for line in raw.splitlines() if line.strip()]
    out = []
    for c in raw[:15]:
        c = str(c).strip()
        if not c:
            continue
        try:
            ast.parse(f"f({c})", mode="eval")
        except SyntaxError:
            raise ToolError(f"case не похож на аргументы вызова: {c!r}")
        out.append(c)
    return out


def _main_guard(node) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and "__name__" in ast.unparse(node.test))


def _module_name(path: Path, ctx) -> str:
    rel = Path(path).resolve().relative_to(Path(ctx.cwd).resolve())
    return ".".join(rel.with_suffix("").parts)


def _call(ctx, module: str, func: str, cases: list[str]) -> list[dict]:
    try:
        p = subprocess.run([sys.executable, "-c", _HARNESS, module, func, json.dumps(cases)],
                           cwd=ctx.cwd, capture_output=True, text=True, timeout=60,
                           env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        raise ToolError("функция не уложилась в 60 секунд на всех случаях")
    marker = p.stdout.rfind("@@JSON@@")
    if marker < 0:
        tail = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["?"]
        raise ToolError(f"не удалось импортировать модуль {module}: {tail[0]}")
    return json.loads(p.stdout[marker + 8:])


def _framework(ctx) -> str:
    found = detect_test_command(Path(ctx.cwd))
    return "unittest" if found and found[1] == "unittest" else "pytest"


def _expect(r: dict, call: str, framework: str) -> list[str]:
    """Строки проверки одного случая."""
    if not r["ok"]:
        exc = r["exc"] if r.get("builtin_exc") and hasattr(builtins, r["exc"]) else "Exception"
        if framework == "pytest":
            lines = [f"with pytest.raises({exc}) as err:", f"    {call}"]
            if exc == "Exception":
                lines.append(f"assert type(err.value).__name__ == {r['exc']!r}")
            return lines
        lines = [f"with self.assertRaises({exc}) as err:", f"    {call}"]
        if exc == "Exception":
            lines.append(f"self.assertEqual(type(err.exception).__name__, {r['exc']!r})")
        return lines
    value = r["repr"]
    if r["type"] == "float" and r["literal"]:
        return [f"assert {call} == pytest.approx({value})"] if framework == "pytest" \
            else [f"self.assertAlmostEqual({call}, {value}, places=9)"]
    if r["literal"]:
        return [f"assert {call} == {value}"] if framework == "pytest" else [f"self.assertEqual({call}, {value})"]
    return [f"assert repr({call}) == {value!r}"] if framework == "pytest" \
        else [f"self.assertEqual(repr({call}), {value!r})"]


def _render(framework: str, module: str, func: str, rows: list[dict]) -> str:
    head = (f'"""Тесты {module}.{func} — сгенерированы loca write_tests.\n\n'
            "Ожидаемые значения получены запуском функции (фиксируют текущее поведение).\n"
            '"""\n')
    if framework == "pytest":
        out = [head, "import pytest", "", f"from {module} import {func}"]
        for i, r in enumerate(rows, 1):
            out += ["", "", f"def test_{func}_{i}():", f"    # {func}({r['case']})"]
            out += ["    " + line for line in _expect(r, f"{func}({r['case']})", "pytest")]
        return "\n".join(out) + "\n"
    cls = "Test" + "".join(p.capitalize() for p in func.split("_"))
    out = [head, "import unittest", "", f"from {module} import {func}", "", "", f"class {cls}(unittest.TestCase):"]
    for i, r in enumerate(rows, 1):
        out += [f"    def test_{func}_{i}(self):", f"        # {func}({r['case']})"]
        out += ["        " + line for line in _expect(r, f"{func}({r['case']})", "unittest")]
        out.append("")
    out += ["", 'if __name__ == "__main__":', "    unittest.main()"]
    return "\n".join(out) + "\n"


def _test_command(framework: str, test_path: Path, ctx) -> str:
    rel = Path(ctx.rel(test_path))
    if framework == "pytest":
        return f"{sys.executable} -m pytest -q -p no:cacheprovider {rel}"
    return f"{sys.executable} -m unittest discover -s {rel.parent or '.'} -p {rel.name} -t . -q"


def _run_tests(cmd: str, ctx, timeout: int = 60) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=ctx.cwd, capture_output=True, text=True, timeout=timeout,
                           env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return p.returncode == 0, (p.stdout + p.stderr)


# ------------------------------------------------------------------ мутации
_CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
             ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
_BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Add, ast.Div: ast.Mult,
             ast.FloorDiv: ast.Div, ast.Mod: ast.FloorDiv}
_SYM = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
        ast.In: "in", ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not", ast.Add: "+",
        ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%"}


def _mutants(fn: ast.FunctionDef) -> list[tuple[int, str, ast.FunctionDef]]:
    """Все одиночные порчи функции: (строка, описание, изменённая копия)."""
    spots = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            for k, op in enumerate(node.ops):
                if type(op) in _CMP_SWAP:
                    spots.append((node, ("cmp", k)))
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in _BIN_SWAP:
            spots.append((node, ("bin", None)))
        elif isinstance(node, ast.BoolOp):
            spots.append((node, ("bool", None)))
        elif isinstance(node, ast.Constant) and type(node.value) is int and abs(node.value) < 10**6:
            spots.append((node, ("int", None)))
        elif isinstance(node, ast.Constant) and type(node.value) is bool:
            spots.append((node, ("not", None)))
        elif isinstance(node, ast.Return) and node.value is not None:
            spots.append((node, ("ret", None)))
    result = []
    index = {id(n): i for i, n in enumerate(ast.walk(fn))}
    for node, (kind, k) in spots:
        clone = copy.deepcopy(fn)
        target = list(ast.walk(clone))[index[id(node)]]
        if kind == "cmp":
            old = type(target.ops[k]); target.ops[k] = _CMP_SWAP[old]()
            what = f"`{_SYM[old]}` → `{_SYM[_CMP_SWAP[old]]}`"
        elif kind == "bin":
            old = type(target.op); target.op = _BIN_SWAP[old]()
            what = f"`{_SYM[old]}` → `{_SYM[_BIN_SWAP[old]]}`"
        elif kind == "bool":
            old = type(target.op); target.op = ast.Or() if old is ast.And else ast.And()
            what = "`and` → `or`" if old is ast.And else "`or` → `and`"
        elif kind == "int":
            what = f"`{target.value}` → `{target.value + 1}`"; target.value += 1
        elif kind == "not":
            what = f"`{target.value}` → `{not target.value}`"; target.value = not target.value
        else:
            what = "`return …` → `return None`"; target.value = None
        clone.decorator_list = []
        result.append((getattr(node, "lineno", fn.lineno), what, clone))
    if len(result) > MAX_MUTANTS:                    # равномерно по функции, а не первые N
        step = len(result) / MAX_MUTANTS
        result = [result[int(i * step)] for i in range(MAX_MUTANTS)]
    return result


def _mutation_check(path: Path, source: str, fn: ast.FunctionDef, cmd: str, ctx):
    """Портит функцию на месте по одной мутации, гоняет тесты, ВСЕГДА возвращает исходник."""
    lines = source.splitlines(keepends=True)
    start, end = fn.lineno - 1, fn.end_lineno
    original_hash = hashlib.sha256(source.encode()).hexdigest()
    killed, survivors, total = 0, [], 0
    try:
        for line, what, mutant in _mutants(fn):
            body = ast.unparse(mutant) + "\n"
            path.write_text("".join(lines[:start]) + body + "".join(lines[end:]), encoding="utf-8")
            ok, _ = _run_tests(cmd, ctx, timeout=30)
            total += 1
            if ok:
                survivors.append((line, what))
            else:
                killed += 1
    finally:
        path.write_text(source, encoding="utf-8")
        if hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest() != original_hash:
            raise ToolError(f"НЕ удалось вернуть {ctx.rel(path)} после мутаций — проверьте файл (/undo)")
    return killed, survivors, total
