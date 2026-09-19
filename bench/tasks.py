"""Задачи мини-бенчмарка: маленькие, проверяемые, из реальной жизни.

Каждая задача — словарь:
  name            короткое имя
  prompt           что просим у агента (как написал бы пользователь)
  files            {путь: содержимое} — рабочая копия проекта
  config           что положить в .loca/config.json задачи (обычно test_command)
  verify_files     {путь: содержимое} — пишутся ПОСЛЕ работы агента, он их не видит
  checks           команды; все должны вернуть 0
  partial          [(метка, команда)] — частичные цели, считаются отдельно
  expect_output    регекс по stdout агента (для задач-вопросов)
  readonly         True — ни один файл не должен измениться (задачи-вопросы)
  tags             ["hard"] — 3+ связанных изменения в разных местах проекта;
                   такая задача обязана иметь минимум 3 частичные цели
  timeout          секунды на задачу (по умолчанию 420)

Задача считается пройденной, если прошли все checks и (если задан) expect_output.

`partial` нужен для слабых моделей: на многофайловой задаче 4B чаще всего
не доходит до конца, и голое «не прошло» ничего не говорит о том, стало ли
лучше. Частичные цели дают градиент: «2/4 подцелей» → «3/4».
"""
from __future__ import annotations

UNITTEST = {"test_command": "python3 -m unittest discover -s tests -t . -q"}

TASKS: list[dict] = [
    {
        "name": "fix_typo",
        "prompt": "В app.py опечатка в выводе: печатается «helo». Исправь и проверь, что программа печатает hello.",
        "files": {"app.py": "def main():\n    print('helo, world')\n\n\nif __name__ == '__main__':\n    main()\n"},
        "checks": ["python3 app.py | grep -q '^hello, world$'"],
    },
    {
        "name": "failing_test",
        "prompt": "Тест падает. Найди причину и исправь код (тест не меняй).",
        "config": UNITTEST,
        "files": {
            "utils.py": (
                "def parse(text):\n"
                '    """Разбирает "a=1;b=2" в словарь."""\n'
                "    out = {}\n"
                "    for part in text.split(';'):\n"
                "        if not part:\n"
                "            continue\n"
                "        key, _, value = part.partition(':')\n"
                "        out[key] = value\n"
                "    return out\n"),
            "tests/__init__.py": "",
            "tests/test_utils.py": (
                "import unittest\n\nfrom utils import parse\n\n\n"
                "class TestParse(unittest.TestCase):\n"
                "    def test_pairs(self):\n"
                "        self.assertEqual(parse('a=1;b=2'), {'a': '1', 'b': '2'})\n\n"
                "    def test_empty(self):\n"
                "        self.assertEqual(parse(''), {})\n"),
        },
        "checks": ["python3 -m unittest discover -s tests -t . -q"],
    },
    {
        "name": "add_cli_flag",
        "prompt": "Добавь в tool.py флаг --json: с ним вместо строки печатается JSON вида {\"value\": 4}. Без флага поведение не меняй.",
        "files": {"tool.py": (
            "import argparse\n\n\n"
            "def main():\n"
            "    p = argparse.ArgumentParser()\n"
            "    p.add_argument('number', type=int)\n"
            "    args = p.parse_args()\n"
            "    print(f'value: {args.number * 2}')\n\n\n"
            "if __name__ == '__main__':\n    main()\n")},
        "verify_files": {"_verify.py": (
            "import json, subprocess, sys\n"
            "out = subprocess.run([sys.executable, 'tool.py', '2', '--json'],\n"
            "                     capture_output=True, text=True, check=True).stdout\n"
            "assert json.loads(out)['value'] == 4, out\n"
            "plain = subprocess.run([sys.executable, 'tool.py', '3'],\n"
            "                       capture_output=True, text=True, check=True).stdout\n"
            "assert plain.strip() == 'value: 6', plain\n"
            "print('ok')\n")},
        "checks": ["python3 _verify.py"],
    },
    {
        "name": "rename_symbol",
        "prompt": "Переименуй функцию calc_total в total_amount везде в проекте. Старого имени остаться не должно, программа должна работать.",
        "files": {
            "pkg/__init__.py": "",
            "pkg/core.py": "def calc_total(items):\n    return sum(items)\n",
            "pkg/report.py": ("from pkg.core import calc_total\n\n\n"
                              "def report(items):\n    return f'total={calc_total(items)}'\n"),
            "main.py": ("from pkg.report import report\n\n"
                        "print(report([10, 20]))\n"),
        },
        "checks": ["python3 main.py | grep -q 'total=30'",
                   "! grep -rq calc_total --include='*.py' --exclude-dir='.loca*' --exclude-dir='__pycache__' ."],
    },
    {
        "name": "write_test",
        "prompt": "На функцию clamp в mathx.py нет теста. Напиши тест в tests/test_mathx.py (unittest), покрой границы, и прогони тесты.",
        "config": UNITTEST,
        "files": {
            "tests/__init__.py": "",
            "mathx.py": ("def clamp(value, low, high):\n"
                         '    """Прижимает value к диапазону [low, high]."""\n'
                         "    if value < low:\n        return low\n"
                         "    if value > high:\n        return high\n"
                         "    return value\n"),
        },
        "verify_files": {"_verify.py": (
            "import pathlib, subprocess, sys\n"
            "src = pathlib.Path('mathx.py').read_text()\n"
            "test = pathlib.Path('tests/test_mathx.py')\n"
            "assert test.is_file() and 'clamp' in test.read_text(), 'нет теста на clamp'\n"
            "run = lambda: subprocess.run([sys.executable, '-m', 'unittest', 'discover',\n"
            "                              '-s', 'tests', '-t', '.', '-q'],\n"
            "                             capture_output=True, text=True).returncode\n"
            "assert run() == 0, 'тест не проходит на исправном коде'\n"
            "# тест должен ловить поломку, а не просто существовать\n"
            "pathlib.Path('mathx.py').write_text(src.replace('return low', 'return high'))\n"
            "broken = run()\n"
            "pathlib.Path('mathx.py').write_text(src)\n"
            "assert broken != 0, 'тест не падает на сломанном clamp'\n"
            "print('ok')\n")},
        "checks": ["python3 _verify.py"],
    },
    {
        "name": "off_by_one",
        "prompt": "Тест пагинации падает. Исправь код (тест не меняй).",
        "config": UNITTEST,
        "files": {
            "paging.py": ("def page(items, number, size=2):\n"
                          '    """Страницы нумеруются с 1."""\n'
                          "    start = number * size\n"
                          "    return items[start:start + size]\n"),
            "tests/__init__.py": "",
            "tests/test_paging.py": (
                "import unittest\n\nfrom paging import page\n\n\n"
                "class TestPage(unittest.TestCase):\n"
                "    def test_first(self):\n"
                "        self.assertEqual(page([1, 2, 3, 4, 5], 1), [1, 2])\n\n"
                "    def test_third(self):\n"
                "        self.assertEqual(page([1, 2, 3, 4, 5], 3), [5])\n"),
        },
        "checks": ["python3 -m unittest discover -s tests -t . -q"],
    },
    {
        "name": "json_config",
        "prompt": "В settings.json поставь port 9090 и debug true. Остальные поля не трогай.",
        "files": {"settings.json": (
            '{\n  "name": "demo",\n  "port": 8080,\n  "debug": false,\n'
            '  "limits": {"rps": 5, "burst": 10}\n}\n')},
        "verify_files": {"_verify.py": (
            "import json\n"
            "d = json.load(open('settings.json'))\n"
            "assert d['port'] == 9090 and d['debug'] is True, d\n"
            "assert d['name'] == 'demo' and d['limits'] == {'rps': 5, 'burst': 10}, d\n"
            "print('ok')\n")},
        "checks": ["python3 _verify.py"],
    },
    {
        "name": "implement_stub",
        "prompt": "Реализуй median в stats.py по докстрингу и прогони тесты.",
        "config": UNITTEST,
        "files": {
            "stats.py": ("def median(values):\n"
                         '    """Медиана списка чисел. Для чётной длины — среднее двух средних.\n\n'
                         '    Пустой список -> ValueError.\n    """\n'
                         "    raise NotImplementedError\n"),
            "tests/__init__.py": "",
            "tests/test_stats.py": (
                "import unittest\n\nfrom stats import median\n\n\n"
                "class TestMedian(unittest.TestCase):\n"
                "    def test_odd(self):\n        self.assertEqual(median([3, 1, 2]), 2)\n\n"
                "    def test_even(self):\n        self.assertEqual(median([4, 1, 3, 2]), 2.5)\n\n"
                "    def test_empty(self):\n"
                "        with self.assertRaises(ValueError):\n            median([])\n"),
        },
        "checks": ["python3 -m unittest discover -s tests -t . -q"],
    },
    {
        "name": "find_definition",
        "prompt": "В каком файле определена функция handle_retry? Ответь только путём к файлу.",
        "files": {
            "net/__init__.py": "",
            "net/client.py": "from net.retry import handle_retry\n\n\ndef get(url):\n    return handle_retry(url)\n",
            "net/retry.py": "def handle_retry(url, attempts=3):\n    return f'{url} x{attempts}'\n",
            "net/pool.py": "POOL_SIZE = 4\n\n\ndef acquire():\n    return POOL_SIZE\n",
        },
        "checks": [],
        "readonly": True,
        "expect_output": r"net/retry\.py",
    },
    {
        "name": "multi_file_field",
        "prompt": "Добавь в модель User поле active (bool, по умолчанию True) и выведи его в render. Тесты подскажут формат.",
        "config": UNITTEST,
        "files": {
            "models.py": ("from dataclasses import dataclass\n\n\n@dataclass\nclass User:\n"
                          "    name: str\n    email: str\n"),
            "views.py": ("from models import User\n\n\n"
                         "def render(user: User) -> str:\n"
                         "    return f'{user.name} <{user.email}>'\n"),
            "tests/__init__.py": "",
            "tests/test_views.py": (
                "import unittest\n\nfrom models import User\nfrom views import render\n\n\n"
                "class TestRender(unittest.TestCase):\n"
                "    def test_active_by_default(self):\n"
                "        self.assertEqual(render(User('Ann', 'a@x.io')), 'Ann <a@x.io> [active]')\n\n"
                "    def test_inactive(self):\n"
                "        self.assertEqual(render(User('Bob', 'b@x.io', False)), 'Bob <b@x.io> [inactive]')\n"),
        },
        "checks": ["python3 -m unittest discover -s tests -t . -q"],
    },
    {
        "name": "extract_helper",
        "prompt": "В report.py дублируется нормализация строк. Вынеси её в одну функцию и используй в обоих местах, поведение не меняй.",
        "config": UNITTEST,
        "files": {
            "report.py": (
                "def titles(rows):\n"
                "    out = []\n"
                "    for row in rows:\n"
                "        text = row.strip().replace('\\t', ' ')\n"
                "        text = ' '.join(text.split()).lower()\n"
                "        out.append(text)\n"
                "    return out\n\n\n"
                "def labels(rows):\n"
                "    out = []\n"
                "    for row in rows:\n"
                "        text = row.strip().replace('\\t', ' ')\n"
                "        text = ' '.join(text.split()).lower()\n"
                "        out.append(text.upper())\n"
                "    return out\n"),
            "tests/__init__.py": "",
            "tests/test_report.py": (
                "import unittest\n\nfrom report import labels, titles\n\n\n"
                "class TestReport(unittest.TestCase):\n"
                "    def test_titles(self):\n"
                "        self.assertEqual(titles([' A\\tB  c ']), ['a b c'])\n\n"
                "    def test_labels(self):\n"
                "        self.assertEqual(labels([' A\\tB  c ']), ['A B C'])\n"),
        },
        "verify_files": {"_verify.py": (
            "import pathlib, subprocess, sys\n"
            "src = pathlib.Path('report.py').read_text()\n"
            "assert src.count(\"' '.join(text.split()).lower()\") <= 1, 'дубль остался'\n"
            "rc = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',\n"
            "                     '-t', '.', '-q'], capture_output=True, text=True).returncode\n"
            "assert rc == 0, 'тесты не проходят'\n"
            "print('ok')\n")},
        "checks": ["python3 _verify.py"],
    },
    {
        "name": "count_todo",
        "prompt": "Сколько всего строк с TODO во всех файлах проекта? Ответь одним числом.",
        "files": {
            "a.py": "# TODO: раз\nx = 1  # TODO: два\n",
            "b.py": "# TODO: три\n",
            "sub/c.py": "# TODO: четыре\n# ничего\n# TODO: пять\n",
            "notes.md": "- TODO: шесть\n",
        },
        "checks": [],
        "readonly": True,
        "expect_output": r"(?<!\d)6(?!\d)",
    },

    # ---------------------------------------------------------------- hard
    # Задачи на 3+ файла: ровно там, где плоский todo_write ломается.
    # У каждой есть partial-цели, чтобы видеть прогресс, а не только «не прошло».
    {
        "name": "hard_width_option",
        "prompt": ("Добавь ограничение ширины ячейки: параметр width (по умолчанию 8) в "
                   "конфиге, обрезку длинных ячеек в format_row и флаг --width в CLI. "
                   "Тесты в tests/ уже описывают нужное поведение — сделай их зелёными, "
                   "сами тесты не меняй."),
        "config": UNITTEST,
        "tags": ["hard"],
        "timeout": 900,
        "files": {
            "app/__init__.py": "",
            "app/config.py": (
                'DEFAULTS = {"sep": " | "}\n\n\n'
                "def load_config(overrides=None):\n"
                "    cfg = dict(DEFAULTS)\n"
                "    for key, value in (overrides or {}).items():\n"
                "        if value is not None:\n"
                "            cfg[key] = value\n"
                "    return cfg\n"),
            "app/format.py": (
                "def format_row(cells, cfg):\n"
                '    """Склеивает ячейки разделителем из конфига."""\n'
                '    return cfg["sep"].join(cells)\n'),
            "app/cli.py": (
                "import argparse\n\n"
                "from app.config import load_config\n"
                "from app.format import format_row\n\n\n"
                "def main():\n"
                "    p = argparse.ArgumentParser()\n"
                '    p.add_argument("cells", nargs="+")\n'
                '    p.add_argument("--sep")\n'
                "    args = p.parse_args()\n"
                '    cfg = load_config({"sep": args.sep})\n'
                "    print(format_row(args.cells, cfg))\n\n\n"
                'if __name__ == "__main__":\n    main()\n'),
            "tests/__init__.py": "",
            "tests/test_format.py": (
                "import unittest\n\n"
                "from app.config import load_config\n"
                "from app.format import format_row\n\n\n"
                "class TestFormat(unittest.TestCase):\n"
                "    def test_width_default(self):\n"
                "        self.assertEqual(load_config()['width'], 8)\n\n"
                "    def test_truncates_long_cell(self):\n"
                "        cfg = load_config({'width': 5})\n"
                "        self.assertEqual(format_row(['abcdefg', 'hi'], cfg), 'abcd… | hi')\n\n"
                "    def test_keeps_short_cell(self):\n"
                "        cfg = load_config({'width': 8})\n"
                "        self.assertEqual(format_row(['abc', 'de'], cfg), 'abc | de')\n"),
            "tests/test_cli.py": (
                "import subprocess\nimport sys\nimport unittest\n\n\n"
                "def run_cli(*args):\n"
                "    return subprocess.run([sys.executable, '-m', 'app.cli', *args],\n"
                "                          capture_output=True, text=True).stdout.strip()\n\n\n"
                "class TestCli(unittest.TestCase):\n"
                "    def test_width_flag(self):\n"
                "        self.assertEqual(run_cli('--width', '4', 'abcdef', 'xy'), 'abc… | xy')\n\n"
                "    def test_default_width(self):\n"
                "        self.assertEqual(run_cli('abcdef', 'xy'), 'abcdef | xy')\n"),
        },
        "checks": ["python3 -m unittest discover -s tests -t . -q"],
        "partial": [
            ("width в конфиге",
             "python3 -c \"from app.config import load_config; assert load_config().get('width') == 8\""),
            ("format_row обрезает",
             "python3 -m unittest discover -s tests -t . -q -p 'test_format.py'"),
            ("флаг --width в CLI",
             "python3 -m unittest discover -s tests -t . -q -p 'test_cli.py'"),
        ],
    },
    {
        "name": "hard_rename_package",
        "prompt": ("Переименуй calc_total в total_amount везде: реализация, реэкспорт в "
                   "pkg/__init__.py, все вызовы и тесты. Старого имени в проекте остаться "
                   "не должно, main.py и тесты должны работать."),
        "config": UNITTEST,
        "tags": ["hard"],
        "timeout": 900,
        "files": {
            "pkg/__init__.py": 'from pkg.core import calc_total\n\n__all__ = ["calc_total"]\n',
            "pkg/core.py": ("def calc_total(items):\n"
                            '    """Сумма позиций. Используется в api и cli."""\n'
                            "    return sum(items)\n"),
            "pkg/api.py": ("from pkg.core import calc_total\n\n\n"
                           "def summary(items):\n"
                           "    return f'total={calc_total(items)}'\n"),
            "pkg/cli.py": ("from pkg import calc_total\n\n\n"
                           "def main(items):\n"
                           "    print(calc_total(items))\n"),
            "main.py": "from pkg.api import summary\n\nprint(summary([10, 20]))\n",
            "tests/__init__.py": "",
            "tests/test_api.py": (
                "import unittest\n\n"
                "from pkg import calc_total\nfrom pkg.api import summary\n\n\n"
                "class TestApi(unittest.TestCase):\n"
                "    def test_summary(self):\n"
                "        self.assertEqual(summary([10, 20]), 'total=30')\n\n"
                "    def test_reexport(self):\n"
                "        self.assertEqual(calc_total([1, 2]), 3)\n"),
        },
        "checks": ["python3 main.py | grep -q '^total=30$'",
                   "! grep -rq calc_total --include='*.py' --exclude-dir='.loca*' --exclude-dir='__pycache__' .",
                   "python3 -m unittest discover -s tests -t . -q"],
        "partial": [
            ("ядро переименовано", "grep -q 'def total_amount' pkg/core.py"),
            ("реэкспорт обновлён", "! grep -q calc_total pkg/__init__.py"),
            ("вызовы обновлены", "! grep -q calc_total pkg/api.py pkg/cli.py main.py"),
            ("тесты обновлены", "! grep -q calc_total tests/test_api.py"),
            ("зелено после переименования",
             "! grep -rq calc_total --include='*.py' --exclude-dir='.loca*' --exclude-dir='__pycache__' . && python3 -m unittest discover -s tests -t . -q"),
        ],
    },
    {
        "name": "hard_extract_module",
        "prompt": ("Валидация в store.py продублирована в двух функциях. Вынеси её в новый "
                   "модуль validators.py — функция validate_user(name, email), кидающая "
                   "ValueError, — и вызови её из обеих функций; поведение не меняй. "
                   "Добавь тест tests/test_validators.py на саму validate_user (валидные "
                   "данные и обе ошибки) и прогони тесты."),
        "config": UNITTEST,
        "tags": ["hard"],
        "timeout": 900,
        "files": {
            "store.py": (
                "USERS = {}\n\n\n"
                "def add_user(name, email):\n"
                "    if not name or not name.strip():\n"
                "        raise ValueError('имя пустое')\n"
                "    if '@' not in email:\n"
                "        raise ValueError('плохой email')\n"
                "    USERS[email] = name\n"
                "    return email\n\n\n"
                "def update_user(email, name):\n"
                "    if not name or not name.strip():\n"
                "        raise ValueError('имя пустое')\n"
                "    if '@' not in email:\n"
                "        raise ValueError('плохой email')\n"
                "    if email not in USERS:\n"
                "        raise KeyError(email)\n"
                "    USERS[email] = name\n"
                "    return email\n"),
            "tests/__init__.py": "",
            "tests/test_store.py": (
                "import unittest\n\nimport store\n\n\n"
                "class TestStore(unittest.TestCase):\n"
                "    def setUp(self):\n        store.USERS.clear()\n\n"
                "    def test_add_and_update(self):\n"
                "        store.add_user('Ann', 'a@x.io')\n"
                "        store.update_user('a@x.io', 'Anna')\n"
                "        self.assertEqual(store.USERS['a@x.io'], 'Anna')\n\n"
                "    def test_bad_email(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            store.add_user('Ann', 'nope')\n\n"
                "    def test_empty_name(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            store.add_user('  ', 'a@x.io')\n"),
        },
        "verify_files": {"_verify.py": (
            "import pathlib, subprocess, sys\n"
            "v = pathlib.Path('validators.py')\n"
            "assert v.is_file(), 'нет validators.py'\n"
            "assert 'def validate_user' in v.read_text(), 'нет validate_user'\n"
            "src = pathlib.Path('store.py').read_text()\n"
            "assert 'validate_user' in src, 'store.py не использует validate_user'\n"
            "assert 'not in email' not in src, 'дубль валидации остался в store.py'\n"
            "t = pathlib.Path('tests/test_validators.py')\n"
            "assert t.is_file() and 'validate_user' in t.read_text(), 'нет теста на validate_user'\n"
            "rc = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',\n"
            "                     '-t', '.', '-q'], capture_output=True, text=True).returncode\n"
            "assert rc == 0, 'тесты не проходят'\n"
            "print('ok')\n")},
        "checks": ["python3 _verify.py"],
        "partial": [
            ("validators.py создан", "test -f validators.py && grep -q 'def validate_user' validators.py"),
            ("store.py использует его", "grep -q validate_user store.py"),
            ("дубль убран", "! grep -q 'not in email' store.py"),
            ("тест на validate_user",
             "test -f tests/test_validators.py && grep -q validate_user tests/test_validators.py"),
            ("зелено после выноса",
             "grep -q validate_user store.py && python3 -m unittest discover -s tests -t . -q"),
        ],
    },
    {
        "name": "hard_two_bugs",
        "prompt": ("Падают три теста: корзина, налог и итог. Найди причины в коде и исправь "
                   "(тесты не меняй), потом прогони все тесты."),
        "config": UNITTEST,
        "tags": ["hard"],
        "timeout": 900,
        "files": {
            "cart.py": ("def subtotal(items):\n"
                        '    """items: [{\'price\': 10.0, \'qty\': 2}] -> сумма price * qty."""\n'
                        "    return sum(item['price'] for item in items)\n"),
            "tax.py": ("def with_tax(amount, rate):\n"
                       '    """Прибавляет налог и округляет до двух знаков."""\n'
                       "    return int(amount * (1 + rate) * 100) / 100\n"),
            "checkout.py": ("from cart import subtotal\nfrom tax import with_tax\n\n\n"
                            "def total(items, rate=0.2):\n"
                            "    return with_tax(subtotal(items), rate)\n"),
            "tests/__init__.py": "",
            "tests/test_cart.py": (
                "import unittest\n\nfrom cart import subtotal\n\n\n"
                "class TestCart(unittest.TestCase):\n"
                "    def test_quantities(self):\n"
                "        items = [{'price': 10.0, 'qty': 2}, {'price': 2.5, 'qty': 4}]\n"
                "        self.assertEqual(subtotal(items), 30.0)\n\n"
                "    def test_empty(self):\n"
                "        self.assertEqual(subtotal([]), 0)\n"),
            "tests/test_tax.py": (
                "import unittest\n\nfrom tax import with_tax\n\n\n"
                "class TestTax(unittest.TestCase):\n"
                "    def test_rounds_up(self):\n"
                "        self.assertEqual(with_tax(9.99, 0.2), 11.99)\n\n"
                "    def test_simple(self):\n"
                "        self.assertEqual(with_tax(10.0, 0.07), 10.7)\n"),
            "tests/test_checkout.py": (
                "import unittest\n\nfrom checkout import total\n\n\n"
                "class TestCheckout(unittest.TestCase):\n"
                "    def test_total(self):\n"
                "        items = [{'price': 10.0, 'qty': 2}, {'price': 2.5, 'qty': 4}]\n"
                "        self.assertEqual(total(items, 0.2), 36.0)\n"),
        },
        "checks": ["python3 -m unittest discover -s tests -t . -q"],
        "partial": [
            ("корзина", "python3 -m unittest discover -s tests -t . -q -p 'test_cart.py'"),
            ("налог", "python3 -m unittest discover -s tests -t . -q -p 'test_tax.py'"),
            ("итог", "python3 -m unittest discover -s tests -t . -q -p 'test_checkout.py'"),
        ],
    },
]

# Группы для --tasks: «core» — прежние мелкие задачи, «hard» — многофайловые.
GROUPS: dict[str, list[str]] = {
    "hard": [t["name"] for t in TASKS if "hard" in t.get("tags", [])],
    "core": [t["name"] for t in TASKS if "hard" not in t.get("tags", [])],
}


def by_name(names=None) -> list[dict]:
    if not names:
        return list(TASKS)
    wanted: set[str] = set()
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        if name in GROUPS:
            wanted.update(GROUPS[name])
        else:
            wanted.add(name)
    picked = [t for t in TASKS if t["name"] in wanted]
    missing = wanted - {t["name"] for t in picked}
    if missing:
        raise SystemExit(f"неизвестные задачи: {', '.join(sorted(missing))}")
    return picked
