"""Persistent MEMORY store for the agent.

Memory is stored as markdown bullet lists in ``memory.md`` (global ~/.ssr and/or
project). Conversation turns are appended to ``.ssr/past_chats.jsonl``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings


class MemoryStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    # --- durable memory.md -------------------------------------------------
    def remember(self, note: str, scope: str = "project") -> str:
        path = self._memory_path(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        line = f"- ({stamp}) {note.strip()}\n"
        with path.open("a", encoding="utf-8") as fh:
            if path.stat().st_size == 0:
                fh.write("# SSR Memory\n\n")
            fh.write(line)
        return f"Remembered ({scope}): {note.strip()}"

    def recall(self, scope: str = "project") -> str:
        path = self._memory_path(scope)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def _memory_path(self, scope: str) -> Path:
        return self.settings.memory_file if scope == "global" else self.settings.project_memory_file

    # --- conversation transcript ------------------------------------------
    def log_turn(self, role: str, content: str) -> None:
        path = self.settings.past_chats_file
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "role": role,
            "content": content,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
