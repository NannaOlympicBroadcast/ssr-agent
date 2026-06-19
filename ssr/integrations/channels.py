"""Shared channel helpers for IM integrations."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import Settings


def push_notification(settings: Settings, message: str, channel: str = "default") -> str:
    """Best-effort channel notification sink.

    Real Feishu/Wechat adapters can tail this queue or implement direct sends;
    this provides a stable builtin tool contract without faking delivery.
    """
    path = settings.home / "channel_notifications.jsonl"
    rec = {"channel": channel, "message": message}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return f"queued notification for {channel} in {path}"
