#!/usr/bin/env python3
"""A/B двух групп прогонов bench/run.py (T-20): A — codeindex выкл, B — вкл.

    python3 bench/run.py --set temperature=0 --out runs/A1.json            # и A2
    python3 bench/run.py --set temperature=0 --set codeindex=true --out runs/B1.json   # и B2
    python3 bench/ab.py --a runs/A1.json runs/A2.json --b runs/B1.json runs/B2.json

Каждый файл — одна попытка всех задач; pass@1 — доля пройденных попыток.
Правило T-20: вливаем, если pass@1 в B не хуже, а медиана шагов или токенов меньше
хотя бы на 10 %. Если в группе есть попытки с tokens_out = 0, замер недействителен.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

INDEX_TOOLS = ("deps", "impact", "review_diff")
GAIN = 0.10


def load(paths: list[str]) -> list[dict]:
    out = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        out += [dict(r, _file=path) for r in data.get("results", [])]
    return out


def _median(values: list) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(nums) if nums else None


def summary(results: list[dict]) -> dict:
    n = len(results) or 1
    return {
        "n": len(results),
        "pass1": sum(1 for r in results if r.get("passed")) / n,
        "steps": _median([r.get("steps") for r in results]),
        "tokens": _median([r.get("tokens_out") for r in results]),
        "errors": sum(int(r.get("tool_errors") or 0) for r in results),
        "index_share": sum(1 for r in results
                           if any((r.get("tools") or {}).get(t) for t in INDEX_TOOLS)) / n,
        "silent": sum(1 for r in results if not r.get("tokens_out")),
    }


def _gain(a: float | None, b: float | None) -> float | None:
    return None if not a or b is None else 1 - b / a


def verdict(a: dict, b: dict) -> tuple[bool, str]:
    if a["silent"] or b["silent"]:
        return False, (f"замер недействителен: попыток с tokens_out = 0 — A {a['silent']}, "
                       f"B {b['silent']}")
    if not a["n"] or not b["n"]:
        return False, "в одной из групп нет результатов"
    if b["pass1"] < a["pass1"]:
        return False, f"не вливаем: pass@1 в B хуже ({b['pass1']:.0%} < {a['pass1']:.0%})"
    gains = {k: _gain(a[k], b[k]) for k in ("steps", "tokens")}
    best = max((g for g in gains.values() if g is not None), default=None)
    if best is not None and best >= GAIN:
        what = ", ".join(f"{k} −{g:.0%}" for k, g in gains.items() if g is not None and g >= GAIN)
        return True, f"вливаем: pass@1 не хуже, {what}"
    note = "" if b["index_share"] else "; модель не вызывала deps/impact/review_diff — нужны трассы (T-12)"
    return False, f"не вливаем: экономии шагов или токенов ≥ {GAIN:.0%} нет{note}"


def table(a: dict, b: dict) -> str:
    def fmt(key, v):
        if v is None:
            return "—"
        return f"{v:.0%}" if key in ("pass1", "index_share") else f"{v:g}"
    rows = [("попыток", "n"), ("pass@1", "pass1"), ("медиана шагов", "steps"),
            ("медиана tokens_out", "tokens"), ("ошибки инструментов", "errors"),
            ("доля с deps/impact/review_diff", "index_share"), ("tokens_out = 0", "silent")]
    lines = ["| метрика | A (выкл) | B (вкл) |", "|---|---|---|"]
    lines += [f"| {title} | {fmt(k, a[k])} | {fmt(k, b[k])} |" for title, k in rows]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="A/B прогонов bench/run.py (T-20)")
    p.add_argument("--a", nargs="+", required=True, help="JSON группы A (флаг выкл), по попытке на файл")
    p.add_argument("--b", nargs="+", required=True, help="JSON группы B (флаг вкл)")
    args = p.parse_args(argv)
    ra, rb = load(args.a), load(args.b)
    names_a, names_b = {r["name"] for r in ra}, {r["name"] for r in rb}
    if names_a != names_b:
        print("⚠ задачи в группах разные: " + ", ".join(sorted(names_a ^ names_b)), file=sys.stderr)
    a, b = summary(ra), summary(rb)
    ok, why = verdict(a, b)
    print(table(a, b))
    print(f"\nрешение: {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
