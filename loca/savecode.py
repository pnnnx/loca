"""Страховка для дообученной маленькой модели: «код показала в чате, а файлов нет».

my-coder v4 (Qwen2.5-Coder-7B + 1581 пример «объяснить/написать код» без единого вызова инструмента)
на «напиши код и тесты» отвечает текстом с ```python-блоками и не вызывает write_file, а на «в какой
директории файлы» выдумывает листинг. Шаблон чата с tools на месте — это выученная привычка, а не
поломка сервера. Поэтому, как и с автозапуском, решаем кодом, а не промптом:
1) просьба создать код + ответ с кодом + ни одной записи файла → одно напоминание вызвать write_file;
2) не помогло → блоки кода раскладываются по файлам детерминированно (через обычный write_file,
   с правами и /undo), к тестам добавляется импорт, тесты запускаются;
3) вопрос про файлы/папки → настоящий list_dir до ответа модели.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

_CREATE_RE = re.compile(
    r"\b(напиши|написать|создай|создать|сделай|сделать|добавь|добавить|реализуй|реализовать|сгенерируй|"
    r"write|create|implement|make|add)\w*\b.{0,80}?"
    r"(код|функци|тест|скрипт|файл|программ|класс|модул|решени|code|function|test|script|file|program|class|module)",
    re.I | re.S)
_LIST_RE = re.compile(
    r"(в как(ой|ую) (папк|директори|каталог)|где (лежат|находятся|сейчас)? ?(файл|код|тест)|"
    r"какие (есть )?файлы|что (лежит )?в (папке|директории|каталоге)|покажи (файлы|папку|директорию)|"
    r"список файлов|\bls\b|which (folder|directory)|what files|list (the )?files)", re.I)
_FENCE_RE = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.S)
_EXT = {"python": "py", "py": "py", "python3": "py", "": "py", "javascript": "js", "js": "js",
        "typescript": "ts", "ts": "ts", "java": "java", "cpp": "cpp", "c++": "cpp", "c": "c", "go": "go",
        "rust": "rs", "sql": "sql", "bash": "sh", "sh": "sh"}
_PATH_RE = re.compile(r"((?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|java|cpp|c|go|rs|sql|sh))")

NUDGE_TEXT = (
    "[loca] You showed code in the chat but did NOT create any files — the user sees no files on disk. "
    "Save it now: call write_file for each file{files}. Tests go to their own file and must import "
    "the code they test. Do not paste the code again in your answer; after writing, run run_tests.")
LIST_TEXT = ("[loca] The real directory listing is above (list_dir). Answer from it only — "
             "never invent files that are not in it.")


def wants_files(text: str) -> bool:
    """Просьба что-то написать/создать (а не объяснить)."""
    low = text or ""
    if "Файлы:" in low and "(новый)" in low:
        return True
    return bool(_CREATE_RE.search(low))


def asks_listing(text: str) -> bool:
    return bool(_LIST_RE.search(text or ""))


def spec_files(text: str) -> list[str]:
    """Пути из строки «Файлы:» задания (loca/spec.py)."""
    for line in (text or "").splitlines():
        if line.strip().lower().startswith("файлы:"):
            return list(dict.fromkeys(_PATH_RE.findall(line)))
    # без задания — пути, названные прямо в просьбе («напиши решение в `solution.py`»)
    return list(dict.fromkeys(_PATH_RE.findall(text or "")))


def code_blocks(answer: str) -> list[tuple[str, str]]:
    out = []
    for m in _FENCE_RE.finditer(answer or ""):
        ext = _EXT.get(m.group(1).lower())
        code = m.group(2).rstrip() + "\n"
        if ext and code.strip() and len(code.strip().splitlines()) >= 2:
            out.append((ext, code))
    return out


def _is_test(code: str) -> bool:
    return bool(re.search(r"^\s*(def test_|class Test|import pytest|import unittest|from unittest)", code, re.M))


def _defs(code: str) -> list[str]:
    return re.findall(r"^(?:def|class)\s+([A-Za-z_]\w*)", code, re.M)


def _strip_demo(code: str) -> str:
    """Демонстрационные print(f(...)) после определений — в модуль не кладём (при импорте из тестов шумят).
    Программу без функций (читает stdin, печатает ответ) не трогаем: там print — это и есть ответ."""
    names = _defs(code)
    if not names:
        return code
    demo = re.compile(r"^(#.*|print\(.*\b(" + "|".join(map(re.escape, names)) + r")\b.*)$")
    lines = code.rstrip("\n").splitlines()
    while lines and not lines[-1].startswith((" ", "\t")) and (demo.match(lines[-1].strip()) or not lines[-1].strip()):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def plan_saves(answer: str, request: str = "") -> list[tuple[str, str]]:
    """Блоки кода → [(путь, содержимое)]. Тесты — в тестовый файл с импортом тестируемого кода."""
    blocks = code_blocks(answer)
    if not blocks:
        return []
    files = spec_files(request)
    test_paths = [f for f in files if PurePosixPath(f).name.startswith("test")]
    code_paths = [f for f in files if f not in test_paths]
    impl = [(e, c) for e, c in blocks if not _is_test(c)]
    tests = [(e, c) for e, c in blocks if _is_test(c)]
    saves: list[tuple[str, str]] = []
    module = None
    if impl:
        ext, code = max(impl, key=lambda b: len(_defs(b[1])) * 1000 + len(b[1]))   # самый полный вариант
        names = _defs(code)
        path = code_paths[0] if code_paths else f"{(names[0] if names else 'main')}.{ext}"
        saves.append((path, _strip_demo(code) if ext == "py" else code))
        module = (path, names)
    if tests:
        ext, code = tests[-1]
        if test_paths:
            tpath = test_paths[0]
        elif module:
            tpath = str(PurePosixPath(module[0]).with_name("test_" + PurePosixPath(module[0]).name))
        else:
            tpath = f"test_main.{ext}"
        if ext == "py" and module and module[0].endswith(".py"):
            mod_path = PurePosixPath(module[0])
            used = [n for n in module[1] if re.search(rf"\b{n}\b", code) and n not in _defs(code)]
            imported = re.search(rf"^\s*(from\s+[\w.]+\s+import|import)\s.*\b({'|'.join(used)})\b", code, re.M) if used else True
            if used and not imported:
                head = ""
                depth = len(PurePosixPath(tpath).parent.parts) - len(mod_path.parent.parts)
                if depth > 0:
                    head = ("import sys\nfrom pathlib import Path\n"
                            f"sys.path.insert(0, str(Path(__file__).resolve().parents[{depth}]))\n")
                pkg = ".".join(mod_path.with_suffix("").parts)
                code = head + f"from {pkg} import {', '.join(used)}\n" + ("\n" if not code.startswith("\n") else "") + code
        saves.append((tpath, code))
    return saves
