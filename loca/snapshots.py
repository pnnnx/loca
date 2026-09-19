"""Снимки файлов перед правками и откат последней правки (/undo).

Маленькая модель ошибается чаще большой, поэтому цена ошибки должна быть низкой.
Перед каждой правкой файл копируется в ~/.loca/snapshots/<сессия>/, журнал лежит
рядом в journal.json — так /undo работает и после /resume.
"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path


class Snapshots:
    def __init__(self, root: Path, keep: int = 200):
        self.root = Path(root)
        self.keep = max(1, int(keep))
        self.journal: list[dict] = []
        self._load()

    # ------------------------------------------------------------------ disk
    @property
    def journal_path(self) -> Path:
        return self.root / "journal.json"

    def _load(self) -> None:
        try:
            data = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, list):
            self.journal = [e for e in data if isinstance(e, dict) and e.get("path")]

    def _save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_text(json.dumps(self.journal, ensure_ascii=False),
                                         encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ api
    def record(self, path: Path, tool: str = "edit") -> None:
        """Сохраняет состояние файла до правки. Ошибки не мешают работе агента."""
        path = Path(path)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            n = (self.journal[-1]["n"] + 1) if self.journal else 1
            entry = {"n": n, "tool": tool, "path": str(path), "existed": path.is_file(),
                     "time": datetime.datetime.now().isoformat(timespec="seconds"),
                     "blob": ""}
            if entry["existed"]:
                blob = self.root / f"{n:05d}_{path.name}"
                shutil.copy2(path, blob)
                entry["blob"] = blob.name
            self.journal.append(entry)
            self._prune()
            self._save()
        except OSError:
            return

    def _prune(self) -> None:
        while len(self.journal) > self.keep:
            old = self.journal.pop(0)
            if old.get("blob"):
                try:
                    (self.root / old["blob"]).unlink()
                except OSError:
                    pass

    def entries(self, limit: int = 10) -> list[dict]:
        return list(reversed(self.journal[-limit:]))

    # ------------------------------------------------------------- метки
    def mark(self) -> int:
        """Метка текущего состояния: всё, что записано позже, можно откатить разом.

        Нужна исполнителю плана: подзадача — это единица работы, и при провале
        откатывать надо её целиком, а не последнюю правку.
        """
        return self.journal[-1]["n"] if self.journal else 0

    def rollback_to(self, mark: int) -> list[Path]:
        """Откатывает всё, что записано после метки. Возвращает затронутые файлы."""
        restored: list[Path] = []
        while self.journal and self.journal[-1]["n"] > mark:
            try:
                path, _what = self.undo()
            except LookupError:
                self.journal.pop()          # снимок потерян — дальше откатывать нечего
                self._save()
                continue
            except OSError:
                break
            restored.append(path)
        return restored

    def undo(self) -> tuple[Path, str]:
        """Откатывает последнюю записанную правку. Возвращает (путь, описание)."""
        if not self.journal:
            raise LookupError("нет записанных правок")
        entry = self.journal[-1]
        path = Path(entry["path"])
        if entry.get("existed"):
            blob = self.root / entry["blob"]
            if not blob.is_file():
                raise LookupError(f"снимок {entry['blob']} потерян")
            shutil.copy2(blob, path)
            what = f"{path.name}: возвращено состояние до {entry['tool']}"
            try:
                blob.unlink()
            except OSError:
                pass
        elif path.is_file():
            path.unlink()
            what = f"{path.name}: файл удалён (он был создан агентом)"
        else:
            what = f"{path.name}: уже отсутствует"
        self.journal.pop()
        self._save()
        return path, what
