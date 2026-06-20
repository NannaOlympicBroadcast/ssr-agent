"""The in-process message bus.

Every running SSR agent owns a :class:`MessageBus`. It is a thread-safe,
topic-based publish/subscribe hub that the agent (and external code) uses to:

* register **listeners** that fire a callback when a matching event arrives,
* **publish** events to communicate with other listeners / agents,
* **wait** (block) for a single event — used to suspend a session until something
  happens elsewhere.

A bus can optionally be bridged to a remote bus server (see
:mod:`ssr.bus.client`), in which case published events are forwarded to the
server and events from the server are injected locally. De-duplication by event
id keeps a bridged event from echoing back and forth.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .events import BusEvent, topic_matches


@dataclass
class Listener:
    id: str
    pattern: str
    callback: Callable[[BusEvent], None]
    once: bool = False
    description: str = ""


class MessageBus:
    """A thread-safe, topic-based pub/sub bus with optional remote bridging."""

    def __init__(self, name: str = "local", source: str = "", history: int = 200):
        self.name = name
        self.source = source or name
        self._listeners: dict[str, Listener] = {}
        self._lock = threading.RLock()
        self._history: deque[BusEvent] = deque(maxlen=history)
        self._seen: deque[str] = deque(maxlen=2048)  # event ids, for de-dup
        self._seen_set: set[str] = set()
        # Forwarder installed by a remote bridge: called with each locally
        # originated event so it can be relayed to the remote server.
        self._forwarder: Callable[[BusEvent], None] | None = None

    # ------------------------------------------------------------- subscribe
    def subscribe(
        self,
        pattern: str,
        callback: Callable[[BusEvent], None],
        once: bool = False,
        description: str = "",
    ) -> str:
        """Register a listener; return its id. ``pattern`` uses topic wildcards."""
        listener_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._listeners[listener_id] = Listener(
                id=listener_id,
                pattern=pattern or "**",
                callback=callback,
                once=once,
                description=description,
            )
        return listener_id

    def unsubscribe(self, listener_id: str) -> bool:
        with self._lock:
            return self._listeners.pop(listener_id, None) is not None

    def listeners(self) -> list[dict]:
        with self._lock:
            return [
                {"id": ls.id, "pattern": ls.pattern, "once": ls.once, "description": ls.description}
                for ls in self._listeners.values()
            ]

    # --------------------------------------------------------------- publish
    def publish(
        self,
        topic: str,
        payload: dict | None = None,
        source: str | None = None,
        correlation_id: str | None = None,
    ) -> BusEvent:
        """Create and dispatch an event; also forward it to a remote bridge."""
        event = BusEvent(
            topic=topic,
            payload=payload or {},
            source=source or self.source,
            correlation_id=correlation_id,
        )
        self._dispatch(event, forward=True)
        return event

    def emit(self, event: BusEvent, forward: bool = True) -> BusEvent:
        """Dispatch a pre-built :class:`BusEvent` (used by transports)."""
        self._dispatch(event, forward=forward)
        return event

    def inject_remote(self, event: BusEvent) -> None:
        """Inject an event received from the remote server (never re-forwarded)."""
        self._dispatch(event, forward=False)

    def _dispatch(self, event: BusEvent, forward: bool) -> None:
        with self._lock:
            if event.id in self._seen_set:
                return  # de-dup: already handled this event id
            self._seen.append(event.id)
            self._seen_set.add(event.id)
            if len(self._seen) >= self._seen.maxlen:
                # keep the set bounded alongside the deque
                self._seen_set = set(self._seen)
            self._history.append(event)
            matched = [ls for ls in self._listeners.values() if topic_matches(ls.pattern, event.topic)]
            forwarder = self._forwarder if forward else None
        # Run callbacks outside the lock so a callback may publish/subscribe.
        for ls in matched:
            try:
                ls.callback(event)
            except Exception:
                pass  # a faulty listener must never break dispatch
            if ls.once:
                self.unsubscribe(ls.id)
        if forwarder is not None:
            try:
                forwarder(event)
            except Exception:
                pass

    # ------------------------------------------------------------------ wait
    def wait_for(
        self,
        pattern: str,
        timeout: float | None = None,
        predicate: Callable[[BusEvent], bool] | None = None,
    ) -> BusEvent | None:
        """Block until an event matching ``pattern`` arrives; return it or ``None``.

        This is the primitive behind "suspend the session until X happens": it
        installs a one-shot listener and waits on an :class:`threading.Event`.
        """
        done = threading.Event()
        box: dict[str, BusEvent] = {}

        def _capture(ev: BusEvent) -> None:
            if predicate is not None and not predicate(ev):
                return
            box["event"] = ev
            done.set()

        listener_id = self.subscribe(pattern, _capture)
        try:
            done.wait(timeout=timeout)
        finally:
            self.unsubscribe(listener_id)
        return box.get("event")

    def history(self, pattern: str = "**", limit: int = 50) -> list[BusEvent]:
        with self._lock:
            events = [e for e in self._history if topic_matches(pattern, e.topic)]
        return events[-limit:]

    # ---------------------------------------------------------------- bridge
    def set_forwarder(self, forwarder: Callable[[BusEvent], None] | None) -> None:
        """Install (or remove) the function that relays local events remotely."""
        with self._lock:
            self._forwarder = forwarder

    @property
    def bridged(self) -> bool:
        with self._lock:
            return self._forwarder is not None
