from pathlib import Path

from loca.config import DEFAULTS
from loca.memory import GRUMPY_PROMPT, system_prompt


def test_grumpy_off_by_default(tmp_path: Path):
    cfg = dict(DEFAULTS)
    assert GRUMPY_PROMPT not in system_prompt(tmp_path, cfg, "default")


def test_grumpy_appends_persona_line(tmp_path: Path):
    cfg = dict(DEFAULTS, grumpy=True)
    text = system_prompt(tmp_path, cfg, "default")
    assert text.rstrip().endswith(GRUMPY_PROMPT)
    # субагенты работают молча и без характера
    assert GRUMPY_PROMPT not in system_prompt(tmp_path, cfg, "default", sub=True)


def test_persona_text_matches_dataset():
    data = Path(__file__).resolve().parents[2] / "coder" / "ds" / "tools" / "persona_data.json"
    if not data.exists():
        return
    import json
    assert json.loads(data.read_text(encoding="utf-8"))["persona"] == GRUMPY_PROMPT
