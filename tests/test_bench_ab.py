"""Стенд A/B для T-20: --set в bench/run.py и сводка bench/ab.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))


def _run(tmp_path, name, rows):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"results": rows}), encoding="utf-8")
    return str(path)


def _row(name, passed, steps, tokens, tools=None, errors=0):
    return {"name": name, "passed": passed, "steps": steps, "tokens_out": tokens,
            "tools": tools or {}, "tool_errors": errors}


def test_set_merges_into_task_config(tmp_path):
    import run
    assert run.parse_set(["codeindex=true", "temperature=0", "model=x"]) == \
        {"codeindex": True, "temperature": 0, "model": "x"}
    with pytest.raises(SystemExit):
        run.parse_set(["codeindex"])
    run.materialize({"files": {"a.py": "x = 1\n"}, "config": {"test_command": "pytest"}},
                    tmp_path, {"codeindex": True})
    assert json.loads((tmp_path / ".loca" / "config.json").read_text()) == \
        {"test_command": "pytest", "codeindex": True}


def test_ab_merge_when_fewer_steps(tmp_path, capsys):
    import ab
    a = [_run(tmp_path, "A1", [_row("t1", True, 10, 500), _row("t2", False, 12, 600)])]
    b = [_run(tmp_path, "B1", [_row("t1", True, 7, 450, {"deps": 1}), _row("t2", True, 8, 500)])]
    assert ab.main(["--a", *a, "--b", *b]) == 0
    out = capsys.readouterr().out
    assert "| pass@1 | 50% | 100% |" in out and "| доля с deps/impact/review_diff | 0% | 50% |" in out
    assert "вливаем" in out and "steps −" in out


def test_ab_rejects_silent_or_worse(tmp_path):
    import ab
    ok = [_row("t1", True, 10, 500)]
    silent = [_row("t1", True, 5, 0)]
    worse = [_row("t1", False, 5, 300)]
    a = ab.summary(ok)
    assert ab.verdict(a, ab.summary(silent))[0] is False
    assert "недействителен" in ab.verdict(a, ab.summary(silent))[1]
    assert "хуже" in ab.verdict(a, ab.summary(worse))[1]
    same = ab.verdict(a, ab.summary([_row("t1", True, 10, 495)]))
    assert same[0] is False and "трассы" in same[1]
