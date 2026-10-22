"""Ревью git diff по индексу кода, без модели (бета codeindex, T-19).

Четыре проверки:
  сигнатура      — параметры функции изменились, а вызывающие передают аргументы по-старому;
  без теста      — изменённую или новую функцию не вызывает ни один тест (до 8 уровней вызовов;
                   методы классов с внешней базой — через создание экземпляра);
  не вызывается  — новую функцию никто не вызывает и нигде не упоминает;
  return None    — появился return None, а вызывающие используют результат.

Новая сторона диффа — рабочая копия (её индексирует CodeIndex), старая восстанавливается
обратным применением диффа, поэтому git для разбора готового диффа не нужен.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .base import Tool, ToolContext, ToolError, obj, truncate
from .codeindex import PY_SUFFIXES, CodeIndex, _is_test_file, load_index, scan_file
from .fs import _read_text

KINDS = {"signature": "сигнатура", "return_none": "return None", "no_test": "без теста",
         "unused": "не вызывается"}
_ORDER = list(KINDS)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DEFS = ("def", "async def")


@dataclass
class Finding:
    kind: str
    file: str
    line: int
    symbol: str
    message: str

    def __str__(self) -> str:
        return f"[{KINDS[self.kind]}] {self.file}:{self.line} {self.symbol} — {self.message}"


@dataclass
class FileDiff:
    path: str
    old_path: str | None                         # None — файл новый
    hunks: list = field(default_factory=list)   # [(new_start, new_count, [(тег, текст)])]


# ------------------------------------------------------------ разбор диффа
def _diff_path(raw: str) -> str | None:
    s = raw.split("\t", 1)[0].strip()
    if s == "/dev/null":
        return None
    return s[2:] if s[:2] in ("a/", "b/") else s


def parse_diff(text: str) -> list[FileDiff]:
    """Unified diff -> файлы с ханками. Удалённые файлы пропускаются."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            new = _diff_path(lines[i + 1][4:])
            cur = FileDiff(new, _diff_path(line[4:])) if new else None
            if cur:
                files.append(cur)
            i += 2
            continue
        m = _HUNK.match(line)
        if m and cur is not None:
            oc, ns, nc = int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
            body, o, n, i = [], 0, 0, i + 1
            while i < len(lines) and (o < oc or n < nc):     # по счётчикам, а не по виду строки
                b = lines[i]
                tag = b[:1] or " "
                if tag == "\\":
                    i += 1
                    continue
                if tag == " ":
                    o, n = o + 1, n + 1
                elif tag == "-":
                    o += 1
                elif tag == "+":
                    n += 1
                else:
                    break
                body.append((tag, b[1:]))
                i += 1
            cur.hunks.append((ns, nc, body))
            continue
        i += 1
    return files


def _reverse(fd: FileDiff, new_lines: list[str]) -> tuple[list[str] | None, set[int]]:
    """Старая версия файла из новой и диффа + номера тронутых строк новой версии."""
    old: list[str] = []
    touched: set[int] = set()
    ptr = 1                                    # следующая строка новой версии (с 1)
    for ns, nc, body in fd.hunks:
        start = ns if nc == 0 else ns - 1      # сколько строк новой версии стоят до ханка
        if start < ptr - 1:
            raise ValueError("ханки не по порядку")
        old += new_lines[ptr - 1:start]
        ptr = start + 1
        for tag, text in body:
            if tag == "-":
                old.append(text)
                touched.add(max(ptr - 1, 1))   # удаление внутри функции метит соседнюю строку
                continue
            if ptr > len(new_lines) or new_lines[ptr - 1] != text:
                raise ValueError(f"строка {ptr} отличается")
            if tag == "+":
                touched.add(ptr)
            else:
                old.append(text)
            ptr += 1
    old += new_lines[ptr - 1:]
    return (None if fd.old_path is None else old), touched


# ------------------------------------------------------------ проверки
def _call_params(rec: dict) -> list[dict]:
    params = list(rec.get("params") or [])
    if (rec.get("cls") and params and params[0]["n"] in ("self", "cls")
            and params[0]["k"] in ("pos", "posonly")):
        params = params[1:]                    # obj.m(x): self не передаётся явно
    return params


def _misfit(params: list[dict], call: dict) -> str | None:
    """Почему вызов не подходит к параметрам; None — подходит или не понять (*args, **kw)."""
    a, kws = call.get("a", -1), call.get("k") or []
    if a < 0 or "**" in kws:
        return None
    pos = [p for p in params if p["k"] in ("posonly", "pos")]
    var = any(p["k"] == "var" for p in params)
    varkw = any(p["k"] == "varkw" for p in params)
    if a > len(pos) and not var:
        return f"позиционных аргументов {a}, а принимает {len(pos)}"
    named = {p["n"] for p in params if p["k"] in ("pos", "kw")}
    extra = [k for k in kws if k not in named]
    if extra and not varkw:
        return "нет параметра " + ", ".join(extra)
    covered = {p["n"] for p in pos[:a]} | set(kws)
    missing = [p["n"] for p in params
               if p["k"] in ("posonly", "pos", "kw") and not p["d"] and p["n"] not in covered]
    if missing:
        return "не передан " + ", ".join(missing)
    return None


def _uniq_callers(idx: CodeIndex, q: str) -> list:
    seen, out = set(), []
    for item in idx.callers(q):
        key = (item[1], item[2]["l"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _who(idx: CodeIndex, cq: str) -> str:
    local = idx.symbols[cq][1]
    return "(уровень модуля)" if local == "<module>" else local


def _is_test_func(idx: CodeIndex, cq: str, crel: str) -> bool:
    return _is_test_file(crel) and idx.symbols[cq][1].rsplit(".", 1)[-1].startswith("test")


TEST_DEPTH = 8


def _framework_class(idx: CodeIndex, rel: str, cls: str | None, depth: int = 0) -> str | None:
    """Класс с базой вне проекта (ast.NodeVisitor, Exception…): его методы может звать фреймворк."""
    if not cls or depth > 6:
        return None
    cq = idx._q(idx.files[rel]["module"], cls)
    rec = idx.symbols.get(cq, ("", "", {}))[2]
    for base in rec.get("bases", []):
        bq = idx.resolve_in(rel, base, None)
        if not bq or bq.startswith("?."):
            return cq
        if _framework_class(idx, idx.symbols[bq][0], idx.symbols[bq][1], depth + 1):
            return cq
    return None


def _reached_by_test(idx: CodeIndex, q: str, depth: int = TEST_DEPTH) -> bool:
    seen, frontier = {q}, [q]
    for _ in range(depth):
        nxt = []
        for x in frontier:
            rel, local, rec = idx.symbols[x]
            items = list(idx.callers(x))
            if "." in local:                   # метод: вызовы с неизвестным получателем тоже
                items += idx.maybe_callers(local.rsplit(".", 1)[-1])
                fw = _framework_class(idx, rel, rec.get("cls"))
                if fw:                         # метод фреймворка работает, когда класс создан
                    items += idx.callers(fw)
            for cq, crel, _ in items:
                if _is_test_func(idx, cq, crel):
                    return True
                if cq not in seen:
                    seen.add(cq)
                    nxt.append(cq)
        if not nxt:
            break
        frontier = nxt
    return False


class _Texts:
    """Строки файлов индекса, читаются один раз за ревью."""

    def __init__(self, idx: CodeIndex):
        self.idx, self.cache = idx, {}

    def lines(self, rel: str) -> list[str]:
        if rel not in self.cache:
            try:
                self.cache[rel] = _read_text(self.idx.root / rel).splitlines()
            except OSError:
                self.cache[rel] = []
        return self.cache[rel]

    def mentioned(self, name: str, rel: str, def_line: int) -> bool:
        pat = re.compile(rf"\b{re.escape(name)}\b")
        for r in self.idx.files:
            for n, text in enumerate(self.lines(r), 1):
                if r == rel and n == def_line:
                    continue
                if pat.search(text):
                    return True
        return False


def _check_file(idx: CodeIndex, rel: str, old_syms: dict | None, touched: set[int],
                texts: _Texts) -> list[Finding]:
    f = idx.files[rel]
    is_test = _is_test_file(rel)
    out: list[Finding] = []
    for local, rec in f["symbols"].items():
        if rec.get("kind") not in _DEFS:
            continue
        if not any(rec["line"] <= n <= rec["end"] for n in touched):
            continue
        q = idx._q(f["module"], local)
        name = local.rsplit(".", 1)[-1]
        before = old_syms.get(local) if old_syms is not None else None
        if before is not None and before.get("kind") not in _DEFS:
            before = None
        dunder = name.startswith("__") and name.endswith("__")
        if before and before.get("params") != rec.get("params"):
            new_p, old_p = _call_params(rec), _call_params(before)
            for cq, crel, c in _uniq_callers(idx, q):
                why = _misfit(new_p, c)
                if why:
                    was = "" if _misfit(old_p, c) else "; к старой сигнатуре подходил"
                    out.append(Finding("signature", crel, c["l"], _who(idx, cq),
                                       f"вызывает {local}{rec['sig']}: {why}{was}"))
        returns = rec.get("returns") or []
        if (before and "None" in returns and "генератор" not in returns
                and "None" not in (before.get("returns") or ["None"])):
            users = list(dict.fromkeys(f"{crel}:{c['l']}" for _, crel, c in _uniq_callers(idx, q)
                                       if c.get("u")))
            if users:
                more = f" … ещё {len(users) - 8}" if len(users) > 8 else ""
                out.append(Finding("return_none", rel, rec["line"], local,
                                   "теперь может вернуть None, а результат используют: "
                                   + ", ".join(users[:8]) + more))
        if is_test or dunder:
            continue
        unused = False
        if old_syms is not None and before is None and name != "main":
            lines = texts.lines(rel)
            decorated = rec["line"] >= 2 and lines[rec["line"] - 2].lstrip().startswith("@")
            framework = _framework_class(idx, rel, rec.get("cls"))
            if (not decorated and not framework and not idx.callers(q)
                    and not idx.maybe_callers(name)):
                unused = not texts.mentioned(name, rel, rec["line"])
            if unused:
                out.append(Finding("unused", rel, rec["line"], local,
                                   "новую функцию никто не вызывает и не упоминает"))
        if not unused and not _reached_by_test(idx, q):
            what = "новую" if before is None and old_syms is not None else "изменённую"
            out.append(Finding("no_test", rel, rec["line"], local,
                               f"{what} функцию не вызывает ни один тест "
                               f"(до {TEST_DEPTH} уровней вызовов)"))
    return out


# ------------------------------------------------------------ сборка
def review(idx: CodeIndex, diff_text: str, untracked: list[str] = ()) -> tuple[list[Finding], list[str], int]:
    """Замечания, примечания (что пропущено и почему) и число просмотренных файлов Python."""
    notes: list[str] = []
    changes: list[tuple[str, dict | None, set[int]]] = []
    for fd in parse_diff(diff_text):
        if not fd.path.endswith(PY_SUFFIXES):
            continue
        if fd.path not in idx.files:
            notes.append(f"{fd.path}: нет в индексе — пропущен")
            continue
        if idx.files[fd.path].get("error"):
            notes.append(f"{fd.path}: не разбирается (SyntaxError) — пропущен")
            continue
        try:
            new_lines = _read_text(idx.root / fd.path).splitlines()
            old_lines, touched = _reverse(fd, new_lines)
        except (OSError, ValueError) as e:
            notes.append(f"{fd.path}: дифф не совпадает с рабочей копией ({e}) — пропущен")
            continue
        if old_lines is None:
            old_syms: dict | None = {}
        else:
            old = scan_file(fd.old_path or fd.path, "\n".join(old_lines) + "\n")
            old_syms = None if old.get("error") else old["symbols"]
            if old_syms is None:
                notes.append(f"{fd.path}: старая версия не разбирается — проверены только тесты")
        changes.append((fd.path, old_syms, touched))
    seen = {c[0] for c in changes}
    for rel in untracked:
        if rel in idx.files and rel not in seen and not idx.files[rel].get("error"):
            n = len(_read_text(idx.root / rel).splitlines())
            changes.append((rel, {}, set(range(1, n + 1))))
    texts = _Texts(idx)
    findings: list[Finding] = []
    for rel, old_syms, touched in changes:
        findings += _check_file(idx, rel, old_syms, touched, texts)
    findings.sort(key=lambda f: (_ORDER.index(f.kind), f.file, f.line))
    return findings, notes, len(changes)


def format_report(findings: list[Finding], notes: list[str], nfiles: int) -> str:
    if not nfiles and not notes:
        return "review: изменений в файлах Python нет"
    head = (f"review: замечаний — {len(findings)} (файлов Python: {nfiles})" if findings
            else f"review: замечаний нет (файлов Python: {nfiles})")
    return "\n".join([head] + [str(f) for f in findings] + [f"примечание: {n}" for n in notes])


def git_diff(cwd: Path, base: str = "HEAD") -> tuple[str, list[str]]:
    """git diff рабочей копии от base (пути от cwd) и неотслеживаемые файлы Python."""
    if not base or base.startswith("-"):
        raise ToolError(f"Недопустимый base: {base!r}")

    def run(cmd: list[str]) -> str:
        try:
            res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(f"git недоступен: {e}") from e
        if res.returncode != 0:
            raise ToolError("git: " + (res.stderr.strip() or "ошибка")[:300])
        return res.stdout

    diff = run(["git", "diff", "--no-color", "--no-ext-diff", "--relative", "-U3", base, "--",
                "*.py", "*.pyi"])
    others = run(["git", "ls-files", "--others", "--exclude-standard", "--", "*.py", "*.pyi"])
    return diff, [ln.strip() for ln in others.splitlines() if ln.strip()]


def run_review(idx: CodeIndex, cwd: Path, base: str = "HEAD",
               diff_text: str | None = None) -> tuple[str, int]:
    untracked: list[str] = []
    if diff_text is None:
        diff_text, untracked = git_diff(cwd, base)
    findings, notes, nfiles = review(idx, diff_text, untracked)
    return format_report(findings, notes, nfiles), len(findings)


class ReviewDiff(Tool):
    name = "review_diff"
    description = ("Ревью изменений без модели, по индексу кода: изменилась сигнатура, а вызывающие "
                   "передают аргументы по-старому; функцию не вызывает ни один тест; новую функцию "
                   "никто не вызывает; появился return None, а результат используют. Вызывайте после "
                   "правок, перед run_tests. Без diff — git diff от base (по умолчанию HEAD) плюс новые "
                   "файлы. Только Python.")
    parameters = obj({
        "base": {"type": "string", "description": "С чем сравнивать: коммит или ветка (по умолчанию HEAD)"},
        "diff": {"type": "string",
                 "description": "Готовый unified diff; его новая сторона должна совпадать с файлами на диске"},
    }, [])

    def summary(self, args):
        return str(args.get("base") or ("diff" if args.get("diff") else "HEAD"))

    def run(self, args, ctx: ToolContext):
        idx = load_index(ctx)
        text, _ = run_review(idx, Path(ctx.cwd), str(args.get("base") or "HEAD"),
                             str(args["diff"]) if args.get("diff") else None)
        return truncate(text, ctx.cfg["max_tool_output"])


def cli_main(argv: list[str]) -> int:
    """loca review [--base REV] [--diff FILE|-] [--cwd DIR]; код 1 — есть замечания, 2 — ошибка."""
    import argparse
    p = argparse.ArgumentParser(prog="loca review",
                                description="Ревью git diff по индексу кода, без модели (бета codeindex)")
    p.add_argument("--base", default="HEAD", help="с чем сравнивать рабочую копию (по умолчанию HEAD)")
    p.add_argument("--diff", metavar="FILE", help="готовый дифф из файла ('-' — из stdin)")
    p.add_argument("--cwd", help="корень проекта")
    a = p.parse_args(argv)
    root = Path(a.cwd or ".").resolve()
    try:
        diff = None
        if a.diff:
            diff = sys.stdin.read() if a.diff == "-" else Path(a.diff).read_text(encoding="utf-8")
        text, n = run_review(CodeIndex(root).refresh(), root, a.base, diff)
    except (ToolError, OSError) as e:
        print(f"loca review: {e}", file=sys.stderr)
        return 2
    print(text)
    return 1 if n else 0
