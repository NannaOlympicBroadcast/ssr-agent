"""Agent-side arm controller: discover capabilities, invoke skills, cache state.

An :class:`ArmController` is a thin shim over an agent's in-process
:class:`~ssr.bus.core.MessageBus` (bridged to the bus server the robot connects
to). It never blocks: it *publishes* a request and returns immediately, and it
caches the robot's capability descriptor and the latest scene snapshot delivered
by ``arm.capabilities`` / ``arm.*.completed`` / ``arm.state`` so the tools (and
the woken checker turn) can read them synchronously.
"""

from __future__ import annotations

import threading
import time
import uuid

from ..bus.core import MessageBus
from ..bus.events import BusEvent
from . import protocol as P


class ArmController:
    def __init__(self, bus: MessageBus, source: str = "ssr-brain"):
        self.bus = bus
        self.source = source
        self._lock = threading.Lock()
        self._caps: dict | None = None
        self._last_completion: dict | None = None
        self._last_state: dict | None = None
        self._last_grasp_result: dict | None = None
        self._episode = 0
        # seq_id of the most recently dispatched action, so we can tell whether a
        # cached completion belongs to the step we're currently waiting on.
        self._pending_seq: str | None = None
        # seq_id of the most recently delegated grasp (cerebellum), same idea.
        self._pending_grasp_seq: str | None = None
        self.bus.subscribe(P.TOPIC_CAPS, self._on_caps)
        self.bus.subscribe(P.TOPIC_ACTION_COMPLETED, self._on_completion)
        self.bus.subscribe(P.TOPIC_GRASP_RESULT, self._on_grasp_result)
        self.bus.subscribe(P.TOPIC_STATE, self._on_state)

    # ------------------------------------------------------------- listeners
    def _on_caps(self, ev: BusEvent) -> None:
        with self._lock:
            self._caps = dict(ev.payload)

    def _on_completion(self, ev: BusEvent) -> None:
        with self._lock:
            self._last_completion = dict(ev.payload)

    def _on_grasp_result(self, ev: BusEvent) -> None:
        with self._lock:
            self._last_grasp_result = dict(ev.payload)

    def _on_state(self, ev: BusEvent) -> None:
        with self._lock:
            self._last_state = dict(ev.payload)

    # --------------------------------------------------------------- publish
    def request_capabilities(self, wait: float = 0.0) -> dict | None:
        self.bus.publish(P.TOPIC_CAPS_REQUEST, {}, source=self.source)
        if wait > 0:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if self.capabilities():
                    break
                time.sleep(0.05)
        return self.capabilities()

    def reset(self) -> str:
        with self._lock:
            self._episode += 1
            self._last_completion = None
            ep = self._episode
        self.bus.publish(P.TOPIC_RESET, {"episode": ep}, source=self.source)
        return f"episode {ep}"

    def execute(self, req: P.ArmActionRequest) -> str:
        """Publish a skill invocation; returns the assigned seq_id immediately."""
        if not req.seq_id:
            req.seq_id = uuid.uuid4().hex[:10]
        with self._lock:
            req.episode = self._episode or 1
            # Clear any prior completion and remember this seq, so completion_ready()
            # only reports the result of *this* step, not a stale earlier one.
            self._pending_seq = req.seq_id
            self._last_completion = None
        self.bus.publish(P.TOPIC_ACTION_EXECUTE, req.to_payload(), source=self.source)
        return req.seq_id

    def grasp(self, req: P.ArmGraspRequest) -> str:
        """Delegate a grasp to the cerebellum; returns the assigned seq_id.

        Non-blocking, like :meth:`execute`: the cerebellum runs its VLX-Flow
        realtime loop and eventually publishes ``arm.grasp.result``, which is
        cached here for :meth:`last_grasp_result` / :meth:`grasp_result_ready`.
        """
        if not req.seq_id:
            req.seq_id = uuid.uuid4().hex[:10]
        with self._lock:
            self._pending_grasp_seq = req.seq_id
            self._last_grasp_result = None
        self.bus.publish(P.TOPIC_GRASP_REQUEST, req.to_payload(), source=self.source)
        return req.seq_id

    def request_state(self) -> None:
        self.bus.publish(P.TOPIC_STATE_REQUEST, {}, source=self.source)

    # ----------------------------------------------------------------- read
    @property
    def episode(self) -> int:
        with self._lock:
            return self._episode

    def capabilities(self) -> dict | None:
        with self._lock:
            return dict(self._caps) if self._caps else None

    def last_completion(self) -> dict | None:
        with self._lock:
            return dict(self._last_completion) if self._last_completion else None

    def completion_ready(self) -> bool:
        """Whether the completion for the most recently dispatched step already
        arrived. The env publishes its completion as soon as the step settles —
        which, for a fast or no-op skill, can happen *before* the agent gets to
        register its completion handler. Since the bus has no replay, that wake
        would be lost forever; callers use this to detect the case and check the
        result directly instead of suspending on an event that already fired."""
        with self._lock:
            c = self._last_completion
            return bool(c and self._pending_seq and c.get("seq_id") == self._pending_seq)

    def last_grasp_result(self) -> dict | None:
        with self._lock:
            return dict(self._last_grasp_result) if self._last_grasp_result else None

    def grasp_result_ready(self) -> bool:
        """Whether the result of the most recently delegated grasp already arrived
        (same race guard as :meth:`completion_ready`, for the cerebellum loop)."""
        with self._lock:
            r = self._last_grasp_result
            return bool(r and self._pending_grasp_seq
                        and r.get("seq_id") == self._pending_grasp_seq)

    def last_state(self) -> dict | None:
        with self._lock:
            return dict(self._last_state) if self._last_state else None

    def latest(self) -> dict | None:
        """Most recent scene snapshot from any source (completion or state)."""
        with self._lock:
            return dict(self._last_completion or self._last_state or {}) or None
