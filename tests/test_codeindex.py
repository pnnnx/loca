"""Бета codeindex: индекс кода, deps и impact (T-18)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT = {
    "pkg/__init__.py": "from .core import total\n",
    "pkg/util.py": "def helper(x):\n    print(x)\n    return x\n",
    "pkg/core.py": (
        "import subprocess\n"
        "from .util import helper\n\n\n"
        "class Error(Exception):\n    pass\n\n\n"
        "def total(items: list, tax: float = 0.2) -> float:\n"
        '    """Сумма с налогом."""\n'
        "    if not items:\n        raise Error('пусто')\n"
        "    items.append(0)\n"
        "    return helper(sum(items)) * (1 + tax)\n\n\n"
        "class Cart:\n"
        "    def __init__(self):\n        self.items = []\n\n"
        "    def add(self, x):\n        self.items.append(x)\n        self.save()\n\n"
        "    def save(self):\n        subprocess.run(['true'])\n\n"
        "    def sum(self):\n        return total(self.items)\n"),
    "app.py": (
        "from pkg import total\nfrom pkg.core import Cart\n\n\n"
        "def main():\n    cart = Cart()\n    cart.add(1)\n    return total([1])\n\n\n"
        "def unused():\n    pass\n\n\n"
        "if __name__ == '__main__':\n    main()\n"),
    "tests/test_core.py": (
        "from pkg.core import Cart, total\n\n\n"
        "def test_total():\n    assert total([1]) > 0\n\n\n"
        "def test_cart():\n    Cart().add(2)\n"),
    "README.md": "# not code\n",
}


@pytest.fixture
def project(tmp_path):
    for rel, text in PROJECT.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def make_ctx(tmp_path, **cfg_over):
    from loca.config import DEFAULTS
    from loca.permissions import Permissions
    from loca.tools import ToolContext
    from loca.ui import UI
    cfg = {**DEFAULTS, "permission_mode": "yolo", **cfg_over}
    return ToolContext(cwd=tmp_path, cfg=cfg, ui=UI(quiet=True), permissions=Permissions(cfg, interactive=False))


def test_codeindex_flag_off_by_default():
    from loca.config import DEFAULTS
    from loca.tools import main_tools, subagent_tools
    assert DEFAULTS["codeindex"] is False
    names = {t.name for t in main_tools(DEFAULTS)}
    assert "deps" not in names and "impact" not in names and "repo_map" in names
    assert {"deps", "impact"} <= {t.name for t in main_tools({**DEFAULTS, "codeindex": True})}
    assert {"deps", "impact"} <= {t.name for t in subagent_tools("research", {"codeindex": True})}
    assert "deps" not in {t.name for t in main_tools()}       # старый вызов без cfg — как раньше


def test_deps_function(project):
    from loca.tools.codeindex import Deps
    out = Deps().run({"symbol": "total"}, make_ctx(project))
    assert "pkg.core.total  (pkg/core.py:9-14)" in out
    assert "принимает: (items: list, tax: float=0.2)" in out
    assert "возвращает: float (по коду: значение)" in out
    assert "исключения: Error" in out
    assert "меняет аргумент items" in out
    assert "pkg.util.helper" in out                        # кого вызывает (через относительный импорт)
    callers = out.split("вызывают (", 1)[1].split("\n", 1)[0]
    assert "app.py:8 main" in callers                      # через реэкспорт pkg/__init__.py
    assert "pkg/core.py:29 Cart.sum" in callers
    assert "tests/test_core.py:5 test_total" in callers
    assert "тесты: tests/test_core.py::test_total" in out and "тесты файла: tests/test_core.py" in out
    assert "импорты файла" in out and "subprocess" in out


def test_deps_methods_effects_and_receivers(project):
    from loca.tools.codeindex import Deps
    ctx = make_ctx(project)
    add = Deps().run({"symbol": "Cart.add"}, ctx)
    assert "меняет self.items" in add and "pkg.core.Cart.save" in add
    assert "app.py:7 main" in add                          # cart = Cart(); cart.add(1)
    assert "tests/test_core.py:9 test_cart" in add         # Cart().add(2)
    assert "процессы: subprocess.run" in Deps().run({"symbol": "pkg/core.py:Cart.save"}, ctx)
    assert "вывод: print" in Deps().run({"symbol": "helper"}, ctx)
    assert "app.py:16 (уровень модуля)" in Deps().run({"symbol": "main"}, ctx)
    assert "вызывают: никто" in Deps().run({"symbol": "unused"}, ctx)
    cls = Deps().run({"symbol": "Cart"}, ctx)
    assert "методы: __init__(), add(x), save(), sum()" in cls and "app.py:6 main" in cls
    from loca.tools.base import ToolError
    with pytest.raises(ToolError):
        Deps().run({"symbol": "nope"}, ctx)


def test_index_records_call_sites_for_review(project):
    from loca.tools.codeindex import CodeIndex
    idx = CodeIndex(project).refresh()
    calls = {c["n"]: c for c in idx.files["pkg/core.py"]["symbols"]["Cart.add"]["calls"]}
    assert calls["self.items.append"]["u"] is False and calls["self.items.append"]["a"] == 1
    total = {c["n"]: c for c in idx.files["pkg/core.py"]["symbols"]["total"]["calls"]}
    assert total["helper"]["u"] is True
    params = idx.files["pkg/core.py"]["symbols"]["total"]["params"]
    assert [(p["n"], p["d"]) for p in params] == [("items", False), ("tax", True)]


def test_index_is_incremental(project):
    from loca.tools.codeindex import CodeIndex
    first = CodeIndex(project).refresh()
    assert len(first.reparsed) == 5 and (project / ".loca" / "index.json").exists()
    again = CodeIndex(project).refresh()                    # новый объект — читает .loca/index.json
    assert again.reparsed == [] and again.files.keys() == first.files.keys()
    util = project / "pkg" / "util.py"
    util.write_text("def helper(x, y=0):\n    return x + y\n", encoding="utf-8")
    st = util.stat()
    os.utime(util, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    (project / "app.py").unlink()
    third = CodeIndex(project).refresh()
    assert third.reparsed == ["pkg/util.py"] and "app.py" not in third.files
    assert "pkg.app.main" not in third.symbols and "app.main" not in third.symbols


def test_impact(project):
    from loca.tools.codeindex import Impact
    out = Impact().run({"files": ["pkg/util.py"]}, make_ctx(project))
    assert out.startswith("изменены: pkg/util.py (1 симв.)")
    lvl1, rest = out.split("уровень 2", 1)
    assert "pkg/core.py: total" in lvl1
    assert "app.py: main" in rest and "tests/test_core.py: test_total" in rest
    assert "импортируют (1): pkg/core.py" in out
    assert "tests/test_core.py::test_total" in out
    assert 'запуск: python3 -m pytest tests/test_core.py -k "test_total"' in out
    one = Impact().run({"files": "pkg/util.py", "depth": 1}, make_ctx(project))
    assert "уровень 2" not in one and "README" not in one
    from loca.tools.base import ToolError
    with pytest.raises(ToolError):
        Impact().run({"files": ["README.md"]}, make_ctx(project))


def _git(root, *cmd):
    import subprocess
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *cmd], cwd=root,
                          check=True, capture_output=True, text=True).stdout


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="нужен git")
def test_changed_files_relative_to_subdir(tmp_path):
    """T-22: из подпапки пути от cwd, а не от корня репозитория (иначе impact без files промахивался)."""
    from loca.tools.codeindex import _changed_files
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("x = 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "sub" / "a.py").write_text("x = 2\n")
    (tmp_path / "sub" / "b.py").write_text("y = 1\n")
    assert sorted(_changed_files(tmp_path / "sub")) == ["a.py", "b.py"]


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="нужен git")
def test_index_cache_hidden_from_git(project):
    """T-22: .loca/index.json не торчит в git status, а config.json проекта — виден."""
    from loca.tools.codeindex import CodeIndex
    _git(project, "init", "-q")
    (project / ".loca").mkdir()
    (project / ".loca" / "config.json").write_text("{}")
    CodeIndex(project).refresh()
    assert (project / ".loca" / "index.json").is_file()
    status = _git(project, "status", "--porcelain", "--untracked-files=all")
    assert ".loca/config.json" in status
    assert "index.json" not in status and ".loca/.gitignore" not in status
    gi = project / ".loca" / ".gitignore"
    gi.write_text("custom\n")                 # чужой .gitignore дополняется, а не затирается
    CodeIndex(project).refresh()._save()
    assert gi.read_text().splitlines() == ["custom", "index.json*"]
