"""Бета codeindex: review_diff — детерминированное ревью диффа (T-19)."""
from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CORE = (
    "def parse(text, sep=','):\n"
    "    return [x.strip() for x in text.split(sep)]\n\n\n"
    "def find(items, key):\n"
    "    for x in items:\n"
    "        if x == key:\n"
    "            return x\n"
    "    return items[0]\n\n\n"
    "def fmt(x):\n"
    "    return str(x)\n")
APP = (
    "from pkg.core import find, fmt, parse\n\n\n"
    "def main(raw):\n"
    "    items = parse(raw, ';')\n"
    "    hit = find(items, 'a')\n"
    "    print(fmt(hit))\n"
    "    return hit\n\n\n"
    "def report(items):\n"
    "    return ', '.join(fmt(x) for x in items)\n\n\n"
    "if __name__ == '__main__':\n"
    "    print(report(main('a;b')))\n")
TESTS = (
    "from app import main\nfrom pkg.core import find, fmt, parse\n\n\n"
    "def test_parse():\n    assert parse('a, b') == ['a', 'b']\n\n\n"
    "def test_find():\n    assert find(['a'], 'a') == 'a'\n\n\n"
    "def test_fmt():\n    assert fmt(1) == '1'\n\n\n"
    "def test_main():\n    assert main('a;b') == 'a'\n")
BASE = {"pkg/__init__.py": "", "pkg/core.py": CORE, "app.py": APP, "tests/test_core.py": TESTS}


def _sub(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new)


# заготовленные диффы: по одному на проверку и один чистый
CASES = {
    "signature": {"pkg/core.py": _sub(CORE, "def parse(text, sep=','):", "def parse(text, *, sep=','):")},
    "no_test": {"app.py": _sub(APP, "fmt(x) for x in items)", "fmt(x) for x in sorted(items))")},
    "unused": {"pkg/core.py": CORE + "\n\ndef tidy(items):\n    return [x for x in items if x]\n"},
    "return_none": {"pkg/core.py": _sub(CORE, "    return items[0]\n", "    return None\n")},
    "clean": {
        "pkg/core.py": _sub(_sub(CORE,
                                 "    return [x.strip() for x in text.split(sep)]\n",
                                 "    parts = text.split(sep)\n    return [x.strip() for x in parts]\n"),
                            "def fmt(x):\n    return str(x)\n",
                            "def fmt(x, upper=False):\n    s = str(x)\n    return s.upper() if upper else s\n")
                       + "\n\ndef count(items):\n    return len(items)\n",
        "app.py": _sub(_sub(APP, "import find, fmt, parse", "import count, find, fmt, parse"),
                       "    print(fmt(hit))\n", "    print(fmt(hit))\n    print(count(items))\n"),
        # новый файл: visit_Name зовёт ast.NodeVisitor, а не код проекта — это не «не вызывается»
        "pkg/walk.py": ("import ast\n\n\nclass Names(ast.NodeVisitor):\n"
                        "    def __init__(self):\n        self.seen = []\n\n"
                        "    def visit_Name(self, node):\n        self.seen.append(node.id)\n\n\n"
                        "def names(src):\n    v = Names()\n    v.visit(ast.parse(src))\n    return v.seen\n"),
        "tests/test_core.py": TESTS + ("\n\ndef test_names():\n    from pkg.walk import names\n"
                                       "    assert names('a') == ['a']\n"),
    },
}


def write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def udiff(old: dict, new: dict) -> str:
    out = []
    for rel in sorted(set(old) | set(new)):
        a, b = old.get(rel), new.get(rel)
        if a == b:
            continue
        out += difflib.unified_diff((a or "").splitlines(True), (b or "").splitlines(True),
                                    "/dev/null" if a is None else f"a/{rel}",
                                    "/dev/null" if b is None else f"b/{rel}")
    return "".join(out)


def run_case(root: Path, case: str):
    from loca.tools.codeindex import CodeIndex
    from loca.tools.review import review
    new = {**BASE, **CASES[case]}
    write(root, new)
    return review(CodeIndex(root).refresh(), udiff(BASE, new))


def make_ctx(tmp_path, **cfg_over):
    from loca.config import DEFAULTS
    from loca.permissions import Permissions
    from loca.tools import ToolContext
    from loca.ui import UI
    cfg = {**DEFAULTS, "permission_mode": "yolo", **cfg_over}
    return ToolContext(cwd=tmp_path, cfg=cfg, ui=UI(quiet=True), permissions=Permissions(cfg, interactive=False))


@pytest.mark.parametrize("case", ["signature", "no_test", "unused", "return_none"])
def test_review_finds_each_check(tmp_path, case):
    findings, notes, nfiles = run_case(tmp_path, case)
    assert notes == [] and nfiles == 1
    assert {f.kind for f in findings} == {case}, [str(f) for f in findings]


def test_review_details(tmp_path):
    (f,), _, _ = run_case(tmp_path / "s", "signature")
    assert (f.file, f.line, f.symbol) == ("app.py", 5, "main")
    assert "позиционных аргументов 2, а принимает 1" in f.message and "к старой" in f.message
    (f,), _, _ = run_case(tmp_path / "n", "no_test")
    assert (f.file, f.symbol) == ("app.py", "report") and "изменённую" in f.message
    (f,), _, _ = run_case(tmp_path / "u", "unused")
    assert (f.file, f.line, f.symbol) == ("pkg/core.py", 16, "tidy")
    (f,), _, _ = run_case(tmp_path / "r", "return_none")
    assert f.symbol == "find" and "app.py:6" in f.message


def test_review_clean_diff_no_false_positives(tmp_path):
    findings, notes, nfiles = run_case(tmp_path, "clean")
    assert findings == [] and notes == [] and nfiles == 4, [str(f) for f in findings]


def test_review_diff_mismatch_is_skipped(tmp_path):
    from loca.tools.codeindex import CodeIndex
    from loca.tools.review import review
    write(tmp_path, BASE)                     # на диске старая версия, дифф — к новой
    findings, notes, _ = review(CodeIndex(tmp_path).refresh(),
                                udiff(BASE, {**BASE, **CASES["signature"]}))
    assert findings == [] and "не совпадает" in notes[0]


def test_parse_diff_counts_not_markers():
    from loca.tools.review import parse_diff
    text = ("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n--- old\n+++ new\n x\n")
    (fd,) = parse_diff(text)
    assert fd.path == "x.py" and fd.hunks[0][2] == [("-", "-- old"), ("+", "++ new"), (" ", "x")]


@pytest.mark.skipif(not shutil.which("git"), reason="нужен git")
def test_review_tool_git_flag_and_cli(tmp_path, capsys):
    from loca.cli import main
    from loca.config import DEFAULTS
    from loca.tools import ToolError, main_tools
    from loca.tools.codeindex import CodeIndex
    from loca.tools.review import run_review
    write(tmp_path, BASE)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "base"]):
        subprocess.run(git + cmd, cwd=tmp_path, check=True, capture_output=True)
    assert "review_diff" not in {t.name for t in main_tools(DEFAULTS)}
    tools = {t.name: t for t in main_tools({**DEFAULTS, "codeindex": True})}
    ctx = make_ctx(tmp_path, codeindex=True)
    assert "изменений в файлах Python нет" in tools["review_diff"].run({}, ctx)
    write(tmp_path, CASES["signature"])
    write(tmp_path, {"pkg/extra.py": "def lonely():\n    return 1\n"})   # новый, не в git
    out = tools["review_diff"].run({}, ctx)
    assert "[сигнатура] app.py:5 main" in out and "[не вызывается] pkg/extra.py:1 lonely" in out
    with pytest.raises(ToolError):
        run_review(CodeIndex(tmp_path).refresh(), tmp_path, "--output=x")
    with pytest.raises(SystemExit) as e:
        main(["review", "--cwd", str(tmp_path)])
    assert e.value.code == 1 and "[сигнатура]" in capsys.readouterr().out
