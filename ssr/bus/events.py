"""Structured bus events and topic matching.

A :class:`BusEvent` is the single unit of communication on the SSR bus. It is a
plain, JSON-serialisable record so it can travel unchanged between the in-process
bus, a remote bus server, and external programs speaking JSON-RPC.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class BusEvent:
    """A single structured event on the bus.

    Attributes:
        topic: Dotted topic name, e.g. ``"task.completed"`` or ``"agent.alice.done"``.
        payload: Arbitrary JSON-serialisable dict carried with the event.
        id: Unique event id (uuid4 hex) — also used for de-duplication across hops.
        source: Free-form identifier of the emitter (agent name, client id, ...).
        ts: Unix timestamp (seconds) when the event was created.
        correlation_id: Optional id linking a response event to a request event.
    """

    topic: str
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source: str = ""
    ts: float = field(default_factory=time.time)
    correlation_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "payload": self.payload,
            "source": self.source,
            "ts": self.ts,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BusEvent":
        if not isinstance(data, dict) or not data.get("topic"):
            raise ValueError("BusEvent requires a 'topic'")
        payload = data.get("payload")
        return cls(
            topic=str(data["topic"]),
            payload=payload if isinstance(payload, dict) else ({} if payload is None else {"value": payload}),
            id=str(data.get("id") or uuid.uuid4().hex),
            source=str(data.get("source") or ""),
            ts=float(data.get("ts") or time.time()),
            correlation_id=data.get("correlation_id"),
        )


def max_message_bytes(default_mb: int = 8) -> int:
    """Maximum WebSocket message size for bus transports, in bytes.

    Bus events may carry images (base64 camera frames / target crops — the
    big-brain→cerebellum grasp protocol relies on this), so the frame limit is
    sized in megabytes and overridable per deployment with ``SSR_BUS_MAX_MSG_MB``
    when higher-resolution cameras need more headroom.
    """
    import os

    raw = os.environ.get("SSR_BUS_MAX_MSG_MB", "")
    try:
        mb = int(raw) if raw.strip() else default_mb
    except ValueError:
        mb = default_mb
    return max(1, mb) * 1024 * 1024


def topic_matches(pattern: str, topic: str) -> bool:
    """Return whether ``topic`` matches a subscription ``pattern``.

    Patterns use dotted segments with two wildcards (MQTT-style):

    * ``*``  matches exactly one segment (``a.*`` ⇒ ``a.b`` but not ``a.b.c``).
    * ``**`` matches the remaining segments, zero or more (``a.**`` ⇒ ``a``,
      ``a.b``, ``a.b.c``). A bare ``*`` or ``**`` matches everything.
    """
    if not pattern or pattern in ("*", "**", "#"):
        return True
    pat = pattern.split(".")
    top = topic.split(".")
    i = 0
    for i, seg in enumerate(pat):
        if seg == "**":
            return True  # greedily matches the rest
        if i >= len(top):
            return False
        if seg == "*":
            continue
        if seg != top[i]:
            return False
    return len(pat) == len(top)
