"""Прицельные проверки узких мест на настоящей модели (быстрее полного бенчмарка).

  python3 bench/probe.py --backend llamacpp --host http://127.0.0.1:8080          # всё
  python3 bench/probe.py --only spec                                              # только №8
  python3 bench/probe.py --only verify --backend ollama -m my-coder-v2            # только №4

№8 «додумывает»   — spec: задание по просьбе. Считаем: формат (6 полей), выдуманные пути,
                     исполняемый критерий «Готово, когда», вопросы/предположения на размытой просьбе.
№4 «не запускает» — verify: «что выведет?» про файл и про кусок кода в вопросе. Считаем: запустил ли
                     код ДО ответа (по трассе) и есть ли в ответе настоящий вывод.
№2 «не держит план» — это полный прогон: bench/run.py --tasks hard (plan_stats в отчёте).

Результат — таблица в консоли и JSON (--out), чтобы сравнивать прогоны и модели.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# ---------------------------------------------------------------- проект-подопытный
FIXTURE = {
    "shop/__init__.py": "",
    "shop/cart.py": (
        '"""Корзина интернет-магазина."""\n\n\n'
        "class Cart:\n"
        "    def __init__(self):\n"
        "        self.items = []          # (sku, price, qty)\n\n"
        "    def add(self, sku: str, price: float, qty: int = 1) -> None:\n"
        "        self.items.append((sku, price, qty))\n\n"
        "    def total(self) -> float:\n"
        "        return sum(p * q for _, p, q in self.items)\n"),
    "shop/report.py": (
        "from shop.cart import Cart\n\n\n"
        "def receipt(cart: Cart) -> str:\n"
        "    lines = [f'{sku} x{qty} = {price * qty}' for sku, price, qty in cart.items]\n"
        "    return '\\n'.join(lines + [f'ИТОГО {cart.total()}'])\n"),
    "tests/__init__.py": "",
    "tests/test_cart.py": (
        "import unittest\nfrom shop.cart import Cart\n\n\n"
        "class CartTest(unittest.TestCase):\n"
        "    def test_total(self):\n"
        "        c = Cart()\n        c.add('a', 10, 2)\n        self.assertEqual(c.total(), 20)\n"),
    "tricky.py": (
        "def add(x, acc=[]):\n"
        "    acc.append(x)\n"
        "    return acc\n\n\n"
        "a = add(1)\nb = add(2)\n"
        "print(a is b, len(a))\n"
        "print(round(2.675, 2), round(0.5), round(1.5))\n"
        "print(sorted({'b': 1, 'a': 2, 'c': 0}, key=lambda k: -ord(k))[:2])\n"),
}
TRICKY_OUT = ["True 2", "2.67 0 2", "['c', 'b']"]

SNIPPET = (
    "x = [1, 2, 3]\n"
    "y = x\n"
    "x += [4]\n"
    "x = x + [5]\n"
    "print(y, x)\n"
    "print(sum(i for i in range(5) if i % 2) or 'пусто')\n")
SNIPPET_OUT = ["[1, 2, 3, 4] [1, 2, 3, 4, 5]", "4"]

SPEC_CASES = [
    {"id": "clear", "ambiguous": False,
     "request": "Добавь в shop/cart.py метод discount(percent), который уменьшает итог на процент, "
                "и тест на него в tests/test_cart.py."},
    {"id": "vague_speed", "ambiguous": True,
     "request": "Сделай, чтобы корзина работала лучше."},
    {"id": "vague_promo", "ambiguous": True,
     "request": "Добавь промокоды."},
    {"id": "wrong_path", "ambiguous": False,
     "request": "Поправь /app/shop/report.py: в чеке итог должен быть с двумя знаками после запятой."},
]

VERIFY_CASES = [
    {"id": "file", "prompt": "Что выведет python3 tricky.py? Объясни каждую строку вывода.",
     "expect": TRICKY_OUT, "run_pattern": r"tricky"},
    {"id": "snippet", "prompt": "Что выведет этот код и почему?\n\n```python\n" + SNIPPET + "```",
     "expect": SNIPPET_OUT, "run_pattern": r"python|snippet|\.py"},
]


def materialize(dst: Path) -> None:
    for rel, text in FIXTURE.items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- №8: задание
FIELDS = ["Цель", "Файлы", "Ограничения", "Готово, когда", "Предположения", "Вопросы"]
PATH_RE = re.compile(r"(?<![\w/])/?(?:[\w.-]+/)*[\w-]+\.(?:py|js|ts|json|toml|md|txt|cfg|ini)\b")
# исполняемый критерий: команда или конкретный тест, а не «функция работает правильно»
RUNNABLE_RE = re.compile(r"pytest|unittest|python3?\s+-|python3?\s+\S+\.py|run_tests|\bnpm\s|\bmake\b|"
                         r"\btest_\w+|tests?/\S+|exit code|код возврата", re.I)


def parse_spec(text: str) -> dict:
    out = {}
    for f in FIELDS:
        m = re.search(rf"^\W*{re.escape(f)}\W*:\s*(.*?)(?=^\W*(?:{'|'.join(map(re.escape, FIELDS))})\W*:|\Z)",
                      text, re.M | re.S)
        if m:
            out[f] = m.group(1).strip()
    return out


def is_none(value: str) -> bool:
    return not value or re.fullmatch(r"[\s«»\"'.-]*(нет|none|-|—)[\s«»\"'.]*", value.lower()) is not None


def score_spec(case: dict, text: str, root: Path) -> dict:
    fields = parse_spec(text)
    files_line = fields.get("Файлы", "")
    invented = []
    for m in PATH_RE.finditer(text):
        rel = m.group(0).lstrip("/")
        if rel.startswith("app/"):
            rel = rel[4:]
        tail = text[m.end():m.end() + 14]
        if (root / rel).exists() or "нов" in tail.lower():
            continue
        invented.append(m.group(0))
    asks = not is_none(fields.get("Вопросы", "")) or not is_none(fields.get("Предположения", ""))
    res = {
        "id": case["id"],
        "format": len(fields) == len(FIELDS),
        "missing": [f for f in FIELDS if f not in fields],
        "invented_paths": sorted(set(invented)),
        "runnable_done": bool(RUNNABLE_RE.search(fields.get("Готово, когда", ""))),
        "asks": asks,
        "files": files_line[:160],
    }
    res["asked_by"] = ("проверка" if "Просьба без подробностей" in fields.get("Вопросы", "")
                       else "модель") if asks else "—"
    if case["ambiguous"]:
        # на размытой просьбе правильно — спросить; критерий тогда появится после ответа
        ok = res["format"] and not res["invented_paths"] and asks
    else:
        ok = res["format"] and not res["invented_paths"] and res["runnable_done"]
    if case["id"] == "wrong_path":
        ok = ok and "shop/report.py" in files_line and "/app/" not in files_line
    res["ok"] = ok
    return res


def probe_spec(args) -> list[dict]:
    from loca.config import load_config
    from loca.llm import make_client
    from loca.permissions import Permissions
    from loca.spec import make_spec
    from loca.tools.base import ToolContext
    from loca.ui import UI

    results = []
    with tempfile.TemporaryDirectory(prefix="loca-probe-spec-") as d:
        root = Path(d)
        materialize(root)
        cfg = load_config(root, overrides(args))
        llm = make_client(cfg)
        ctx = ToolContext(cwd=root, cfg=cfg, ui=UI(quiet=True), permissions=Permissions(cfg, interactive=False))
        for case in [c for c in SPEC_CASES if not args.case or c["id"] == args.case]:
            started = time.time()
            try:
                text = make_spec(llm, ctx, case["request"])
            except Exception as e:  # noqa: BLE001
                text = f"[ошибка] {e}"
            res = score_spec(case, text, root)
            res["seconds"] = round(time.time() - started, 1)
            res["text"] = text
            results.append(res)
            print(f"  spec/{case['id']:<12} {'✓' if res['ok'] else '✗'}  {res['seconds']}s  "
                  f"формат={'да' if res['format'] else 'нет ' + ','.join(res['missing'])} "
                  f"выдумано={res['invented_paths'] or '—'} критерий={'да' if res['runnable_done'] else 'нет'} "
                  f"спрашивает={res['asked_by'] if res['asks'] else 'нет'}", flush=True)
            if args.verbose:
                print("    " + text.replace("\n", "\n    "))
    return results


# ---------------------------------------------------------------- №4: сначала запусти
def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def score_verify(case: dict, stats: dict, answer: str) -> dict:
    trace = stats.get("trace", [])
    runs = [t for t in trace if t.get("tool") in ("bash", "run_tests")
            and re.search(case["run_pattern"], str(t.get("args", "")), re.I)]
    found = [line for line in case["expect"] if norm(line) in norm(answer)]
    res = {"id": case["id"], "ran_code": bool(runs), "real_output": len(found) == len(case["expect"]),
           "found": f"{len(found)}/{len(case['expect'])}", "tools": stats.get("tools", {}),
           "steps": stats.get("steps"), "tokens_in": stats.get("tokens_in")}
    res["ok"] = res["ran_code"] and res["real_output"]
    return res


def probe_verify(args) -> list[dict]:
    results = []
    for case in [c for c in VERIFY_CASES if not args.case or c["id"] == args.case]:
        work = Path(tempfile.mkdtemp(prefix=f"loca-probe-{case['id']}-"))
        home = Path(tempfile.mkdtemp(prefix="loca-probe-home-"))
        try:
            materialize(work)
            stats_path = home / "stats.json"
            cmd = [sys.executable, "-m", "loca", "-p", "--yolo", "--cwd", str(work),
                   "--stats", str(stats_path)]
            for flag, key in (("--backend", "backend"), ("--host", "host"), ("-m", "model"), ("--ctx", "ctx")):
                if getattr(args, key):
                    cmd += [flag, str(getattr(args, key))]
            env = {**os.environ, "PYTHONPATH": str(REPO), "LOCA_HOME": str(home)}
            if args.no_verify_first:
                (work / ".loca").mkdir(exist_ok=True)
                (work / ".loca" / "config.json").write_text('{"verify_first": false}')
            started = time.time()
            try:
                p = subprocess.run(cmd + [case["prompt"]], cwd=work, env=env, capture_output=True,
                                   text=True, timeout=args.timeout, stdin=subprocess.DEVNULL)
                answer = p.stdout
            except subprocess.TimeoutExpired as e:
                answer = e.stdout if isinstance(e.stdout, str) else ""
            stats = {}
            if stats_path.is_file():
                stats = json.loads(stats_path.read_text(encoding="utf-8"))
            res = score_verify(case, stats, answer)
            res["seconds"] = round(time.time() - started, 1)
            res["answer"] = answer[-3000:]
            results.append(res)
            print(f"  verify/{case['id']:<10} {'✓' if res['ok'] else '✗'}  {res['seconds']}s  "
                  f"запустил={'да' if res['ran_code'] else 'нет'} настоящий вывод={res['found']} "
                  f"инструменты={res['tools']}", flush=True)
            if args.verbose:
                print("    " + answer.strip()[-1500:].replace("\n", "\n    "))
        finally:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)
    return results


def overrides(args) -> dict:
    return {"backend": args.backend, "host": args.host, "model": args.model, "num_ctx": args.ctx}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["spec", "verify"])
    ap.add_argument("--case", help="только этот случай (id), например file")
    ap.add_argument("--backend", choices=["ollama", "llamacpp"])
    ap.add_argument("--host")
    ap.add_argument("-m", "--model")
    ap.add_argument("--ctx", type=int)
    ap.add_argument("--timeout", type=int, default=1800, help="секунд на один verify-прогон")
    ap.add_argument("--no-verify-first", action="store_true", help="для сравнения: без напоминания")
    ap.add_argument("--out", help="записать результаты в JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    report = {"started": time.strftime("%Y-%m-%d %H:%M"), "args": vars(args)}
    if args.only in (None, "spec"):
        print("№8 задание перед работой:", flush=True)
        report["spec"] = probe_spec(args)
    if args.only in (None, "verify"):
        print("№4 сначала запусти" + (" (напоминание ВЫКЛ)" if args.no_verify_first else "") + ":", flush=True)
        report["verify"] = probe_verify(args)
    for key in ("spec", "verify"):
        if key in report:
            ok = sum(r["ok"] for r in report[key])
            print(f"{key}: {ok}/{len(report[key])}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("записано:", args.out)


if __name__ == "__main__":
    main()
