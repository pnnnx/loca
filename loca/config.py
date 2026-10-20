"""Configuration: defaults <- ~/.loca/config.json <- ./.loca/config.json <- CLI flags."""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME_DIR = Path(os.environ.get("LOCA_HOME", Path.home() / ".loca"))

DEFAULTS = {
    # ollama | llamacpp. Для llamacpp host — адрес llama-server (по умолчанию :8080),
    # контекст и модель задаются при запуске сервера (см. bin/llama.sh)
    "backend": os.environ.get("LOCA_BACKEND", "ollama"),
    "host": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
    "llamacpp_host": os.environ.get("LLAMA_HOST", "http://127.0.0.1:8080"),
    "max_output_tokens": 4096,   # потолок длины одного ответа модели (llama.cpp)
    "model": "qwen3.5:9b",
    # 16k context fits a 16 GB Mac with a 9B model if OLLAMA_KV_CACHE_TYPE=q8_0;
    # on 8 GB use a smaller model (qwen3.5:4b) or lower num_ctx
    "num_ctx": 16384,
    "temperature": 0.2,
    "think": False,            # qwen3 hybrid thinking; slower but smarter
    "think_smart": True,       # думать точечно: на шаге планирования и после ошибки/падения тестов
    "keep_alive": "30m",
    "request_timeout": int(os.environ.get("LOCA_REQUEST_TIMEOUT", 600)),
    "permission_mode": "default",   # default | accept-edits | plan | yolo
    "max_steps": 40,
    "max_seconds": 0,            # лимит времени на один ход (0 — без лимита)
    "trace_len": 60,             # сколько последних вызовов держать в метриках
    "plan_gate_steps": 6,        # после скольких шагов без плана требовать plan_write
    "plan_gate_files": 2,        # и скольких правленых файлов
    "subagent_max_steps": 15,       # research subagent
    "code_subagent_max_steps": 25,  # coding subagent
    "reminder_every": 6,            # re-inject the plan every N agent steps
    # --- граф подзадач (plan_write / run_plan) -------------------------------
    "plan_min_nodes": 3,         # меньше подзадач — работать самому, без субагентов
    "plan_min_files": 2,         # и меньше файлов тоже: делегирование не бесплатно
    "plan_max_attempts": 2,      # попыток на подзадачу, потом откат и возврат в планирование
    "max_plan_subagents": 12,    # предохранитель на один ход
    "plan_rollback": True,       # откатывать правки провалившейся подзадачи
    "plan_outline_files": 3,     # сколько файлов подзадачи класть в контракт картой символов
    "accept_timeout": 180,       # секунд на одну проверку приёмки
    "test_command": "",             # override test detection, e.g. "make test"
    "vars_limit": 4000,             # max chars kept in one variable
    "max_vars": 40,
    "max_tool_output": 8000,     # chars kept from a single tool result
    "old_tool_output": 600,      # chars kept from tool results older than keep_recent_turns
    "keep_recent_turns": 3,
    "compact_threshold": 0.75,   # share of num_ctx that triggers auto-compaction
    "allow": ["run_tests"],      # e.g. ["bash(npm test*)", "edit_file"]
    "deny": [],
    "mcp_servers": {},           # {"name": {"command": "...", "args": [], "env": {}, "tools": []}}
    "memory_files": ["LOCA.md", "AGENTS.md", "CLAUDE.md"],
    # команды вокруг действий агента: {"after_edit": ["ruff format {file}"], "stop": ["pytest -q"]}
    "hooks": {},
    "hook_timeout": 120,
    "snapshots_keep": 200,       # сколько правок хранить для /undo
    # задание перед работой: auto — для просьб, меняющих файлы; always; off.
    # Только в диалоге: в -p (и в бенчмарке) подтверждать некому.
    "spec": "auto",
    "verify_first": True,
    "style": "",                 # стиль ответов: "teen" или свой текст (/style)
    "think_aloud": False,        # «думать вслух»: короткая мысль серым перед каждым шагом (--think-aloud)
    "grumpy": False,             # режим «ворчун» (/grumpy): реакции с матом — для дообученной модели        # вопросы «что выведет / почему падает» — сначала запустить код
}


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        print(f"[loca] ошибка в {path}: {e}")
        return {}


def load_config(cwd: Path, overrides: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    for path in (HOME_DIR / "config.json", cwd / ".loca" / "config.json"):
        data = _load(path)
        for key, value in data.items():
            # list-valued permission rules are merged, everything else overrides
            if key in ("allow", "deny") and isinstance(value, list):
                cfg[key] = list(cfg.get(key, [])) + value
            elif key in ("mcp_servers", "hooks") and isinstance(value, dict):
                cfg[key] = {**cfg.get(key, {}), **value}
            else:
                cfg[key] = value
    for key, value in (overrides or {}).items():
        if value is not None:
            cfg[key] = value
    for key in ("allow", "deny"):          # правила могут задваиваться после слияния
        seen, unique = set(), []
        for rule in cfg.get(key, []):
            if rule not in seen:
                seen.add(rule)
                unique.append(rule)
        cfg[key] = unique
    return cfg


def save_user_setting(key: str, value) -> Path:
    """Запомнить настройку в ~/.loca/config.json (остальные ключи не трогаем)."""
    path = HOME_DIR / "config.json"
    data = _load(path)
    if value in (None, "", "off"):
        data.pop(key, None)
    else:
        data[key] = value
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def ensure_home() -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    (HOME_DIR / "sessions").mkdir(exist_ok=True)
    (HOME_DIR / "snapshots").mkdir(exist_ok=True)
    cfg_path = HOME_DIR / "config.json"
    if not cfg_path.exists():
        sample = {k: DEFAULTS[k] for k in ("model", "num_ctx", "temperature", "think", "permission_mode")}
        sample["allow"] = ["bash(npm test*)", "bash(pytest*)"]
        sample["mcp_servers"] = {}
        cfg_path.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
