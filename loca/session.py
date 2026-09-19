"""Save / resume conversations in ~/.loca/sessions."""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path

from .config import HOME_DIR


class Session:
    def __init__(self, cwd: Path, session_id: str | None = None):
        self.id = session_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.cwd = str(cwd)
        self.created = datetime.datetime.now().isoformat(timespec="seconds")
        self.title = ""

    @property
    def path(self) -> Path:
        return HOME_DIR / "sessions" / f"{self.id}.json"

    def save(self, messages: list[dict], plan: list[dict],
             variables: dict | None = None) -> None:
        # the system prompt is rebuilt on load, so skip it
        body = [m for m in messages if m.get("role") != "system"]
        if not body:
            return
        if not self.title:
            first = next((m["content"] for m in body if m.get("role") == "user"), "")
            self.title = first.strip().splitlines()[0][:80] if first.strip() else ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"id": self.id, "cwd": self.cwd, "created": self.created, "title": self.title,
                "updated": datetime.datetime.now().isoformat(timespec="seconds"),
                "messages": body, "plan": plan or [], "vars": variables or {}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def load(cls, session_id: str) -> tuple["Session", list[dict], list[dict], dict]:
        path = HOME_DIR / "sessions" / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        s = cls(Path(data["cwd"]), data["id"])
        s.created, s.title = data.get("created", s.created), data.get("title", "")
        return s, data.get("messages", []), data.get("plan", []), data.get("vars", {})


def list_sessions(cwd: Path | None = None, limit: int = 15) -> list[dict]:
    folder = HOME_DIR / "sessions"
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if cwd is not None and data.get("cwd") != str(cwd):
            continue
        out.append({"id": data["id"], "title": data.get("title", ""),
                    "updated": data.get("updated", ""), "turns":
                    sum(1 for m in data.get("messages", []) if m.get("role") == "user")})
        if len(out) >= limit:
            break
    return out
