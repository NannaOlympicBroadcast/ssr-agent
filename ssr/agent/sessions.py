"""Conversation session recording.

Each session is a single append-only ``<id>.jsonl`` file under
``~/.ssr/sessions``. The first line is a ``meta`` record (id, created time,
title, working directory); every later line is a ``turn`` (user/assistant). This
append-only design is process- and thread-safe without locks: writers only ever
append, and listings are rebuilt by scanning the directory.

The store powers two things:

* normal ``ssr`` usage — the REPL / one-shot / dispatched agent runs all record
  their turns, so a machine accumulates a browsable history;
* the dispatch server's web chat, which starts/continues sessions on a node and
  lists/reads a node's past sessions.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings

_TITLE_MAX = 80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, settings: Settings):
        self.dir = settings.home / "sessions"

    # ------------------------------------------------------------- lifecycle
    def create(self, title: str = "", cwd: str = "") -> str:
        """Create a new session and return its id."""
        self.dir.mkdir(parents=True, exist_ok=True)
        sid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        meta = {
            "type": "meta",
            "id": sid,
            "created": _now(),
            "title": _clean_title(title),
            "cwd": cwd,
        }
        with self._path(sid).open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        return sid

    def append(self, session_id: str, role: str, content: str) -> None:
        """Append one conversation turn to a session."""
        path = self._path(session_id)
        if not path.exists():
            # Be forgiving: recreate a bare session file if it vanished.
            self.dir.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"type": "meta", "id": session_id, "created": _now(), "title": "", "cwd": ""},
                    ensure_ascii=False) + "\n")
        rec = {"type": "turn", "ts": _now(), "role": role, "content": content}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False

    # ------------------------------------------------------------- accessors
    def list(self) -> list[dict]:
        """Return session metadata (newest first) with turn counts."""
        if not self.dir.is_dir():
            return []
        out: list[dict] = []
        for path in self.dir.glob("*.jsonl"):
            meta = self._read_meta(path)
            if meta is not None:
                out.append(meta)
        out.sort(key=lambda m: m.get("updated") or m.get("created") or "", reverse=True)
        return out

    def get(self, session_id: str) -> dict | None:
        """Return ``{**meta, "turns": [...]}`` for a session, or None."""
        path = self._path(session_id)
        if not path.exists():
            return None
        meta: dict = {"id": session_id}
        turns: list[dict] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "meta":
                meta.update({k: v for k, v in rec.items() if k != "type"})
            elif rec.get("type") == "turn":
                turns.append({"ts": rec.get("ts"), "role": rec.get("role"), "content": rec.get("content")})
        meta["turns"] = turns
        return meta

    # --------------------------------------------------------------- helpers
    def _path(self, session_id: str) -> Path:
        # Guard against path traversal in ids coming over the wire.
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return self.dir / f"{safe}.jsonl"

    def _read_meta(self, path: Path) -> dict | None:
        meta: dict | None = None
        turns = 0
        last_ts = ""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") == "meta":
                        meta = {k: v for k, v in rec.items() if k != "type"}
                    elif rec.get("type") == "turn":
                        turns += 1
                        last_ts = rec.get("ts") or last_ts
                        if meta is not None and not meta.get("title") and rec.get("role") == "user":
                            meta["title"] = _clean_title(rec.get("content", ""))
        except OSError:
            return None
        if meta is None:
            return None
        meta["turns"] = turns
        meta["updated"] = last_ts or meta.get("created", "")
        return meta


def _clean_title(text: str) -> str:
    title = " ".join((text or "").split())
    return title[:_TITLE_MAX] + ("…" if len(title) > _TITLE_MAX else "")
