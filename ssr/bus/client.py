"""Programmatic bus client + remote bridge.

:class:`BusClient` is a small, synchronous-friendly client that external
programs or other AI agents import to talk to a remote bus server::

    from ssr.bus import BusClient

    client = BusClient("ws://localhost:8765", source="my-script")
    client.connect()
    client.publish("task.started", {"id": 42})
    client.subscribe("task.*", lambda ev: print("got", ev.topic, ev.payload))
    ev = client.wait_for("task.done", timeout=30)   # block for one event
    client.close()

It runs a private asyncio event loop on a background thread, so callers never
have to touch ``async``/``await``. JSON-RPC framing is handled internally.

:class:`RemoteBusBridge` wires a local :class:`MessageBus` to a remote server so
that an agent's built-in bus transparently shares events with everyone else.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Callable

from . import jsonrpc
from .core import MessageBus
from .events import BusEvent, topic_matches


class BusClient:
    """Synchronous-friendly JSON-RPC bus client over WebSocket."""

    def __init__(self, url: str, source: str = "", connect_timeout: float = 10.0):
        self.url = url
        self.source = source or f"client:{uuid.uuid4().hex[:6]}"
        self.connect_timeout = connect_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._handlers: dict[str, tuple[str, Callable[[BusEvent], None]]] = {}
        self._ready = threading.Event()
        self._closed = False
        self._connect_error: Exception | None = None

    # ----------------------------------------------------------- lifecycle
    def connect(self) -> "BusClient":
        """Open the connection and block until it is ready (or raise)."""
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(self.connect_timeout):
            raise TimeoutError(f"bus connect to {self.url} timed out")
        if self._connect_error is not None:
            raise self._connect_error
        return self

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            self._connect_error = e
            self._ready.set()

    async def _main(self) -> None:
        import websockets

        async with websockets.connect(self.url, max_size=8 * 1024 * 1024) as ws:
            self._ws = ws
            self._ready.set()
            async for raw in ws:
                await self._on_message(raw)

    async def _on_message(self, raw: str) -> None:
        try:
            msg = jsonrpc.loads(raw)
        except Exception:
            return
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(str(msg["id"]), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if msg.get("method") == "bus.event":
            event = BusEvent.from_dict((msg.get("params") or {}).get("event") or {})
            for pattern, handler in list(self._handlers.values()):
                if topic_matches(pattern, event.topic):
                    try:
                        handler(event)
                    except Exception:
                        pass

    # --------------------------------------------------------------- rpc
    def _call(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        if self._loop is None or self._ws is None:
            raise RuntimeError("bus client is not connected")
        rid = uuid.uuid4().hex

        async def _send_and_wait():
            fut = self._loop.create_future()
            self._pending[rid] = fut
            await self._ws.send(jsonrpc.dumps(jsonrpc.request(method, params, id=rid)))
            return await asyncio.wait_for(fut, timeout)

        future = asyncio.run_coroutine_threadsafe(_send_and_wait(), self._loop)
        msg = future.result(timeout + 5)
        if "error" in msg:
            err = msg["error"]
            raise jsonrpc.JsonRpcError(err.get("code", jsonrpc.BUS_ERROR), err.get("message", "bus error"))
        return msg.get("result") or {}

    # ------------------------------------------------------------- public
    def publish(
        self,
        topic: str,
        payload: dict | None = None,
        correlation_id: str | None = None,
        event_id: str | None = None,
    ) -> dict:
        """Publish an event to the remote bus; return the broker's event record.

        ``event_id`` is normally omitted (the server assigns one). The bridge
        passes the local event's id so the server-echoed copy de-duplicates.
        """
        params = {
            "topic": topic,
            "payload": payload or {},
            "source": self.source,
            "correlation_id": correlation_id,
        }
        if event_id is not None:
            params["id"] = event_id
        result = self._call("bus.publish", params)
        return result.get("event", {})

    def subscribe(self, pattern: str, handler: Callable[[BusEvent], None]) -> str:
        """Register a local handler and tell the server to stream matching events."""
        result = self._call("bus.subscribe", {"pattern": pattern})
        sub_id = result.get("subscription_id") or uuid.uuid4().hex[:12]
        self._handlers[sub_id] = (pattern, handler)
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        self._handlers.pop(subscription_id, None)
        try:
            return bool(self._call("bus.unsubscribe", {"subscription_id": subscription_id}).get("ok"))
        except Exception:
            return False

    def wait_for(self, pattern: str, timeout: float | None = None) -> BusEvent | None:
        """Block until one matching event arrives from the server; return it."""
        done = threading.Event()
        box: dict[str, BusEvent] = {}

        def _capture(ev: BusEvent) -> None:
            box.setdefault("event", ev)
            done.set()

        sub_id = self.subscribe(pattern, _capture)
        try:
            done.wait(timeout=timeout)
        finally:
            self.unsubscribe(sub_id)
        return box.get("event")

    def history(self, pattern: str = "**", limit: int = 50) -> list[BusEvent]:
        result = self._call("bus.history", {"pattern": pattern, "limit": limit})
        return [BusEvent.from_dict(e) for e in result.get("events", [])]

    def ping(self) -> dict:
        return self._call("bus.ping", {})

    def close(self) -> None:
        self._closed = True
        if self._loop is not None and self._ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop).result(5)
            except Exception:
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


class RemoteBusBridge:
    """Connects a local :class:`MessageBus` to a remote bus server.

    Local events are forwarded to the server; events from the server are injected
    into the local bus. Event ids de-duplicate so a forwarded event that the
    server echoes back is not re-dispatched.
    """

    def __init__(self, bus: MessageBus, url: str, pattern: str = "**"):
        self.bus = bus
        self.url = url
        self.pattern = pattern
        self.client = BusClient(url, source=bus.source)

    def start(self) -> "RemoteBusBridge":
        self.client.connect()
        # Inject remote events locally (de-dup happens inside the bus).
        self.client.subscribe(self.pattern, self.bus.inject_remote)

        # Forward locally originated events to the server.
        def _forward(event: BusEvent) -> None:
            try:
                self.client.publish(event.topic, event.payload, event.correlation_id, event_id=event.id)
            except Exception:
                pass

        self.bus.set_forwarder(_forward)
        return self

    def stop(self) -> None:
        self.bus.set_forwarder(None)
        self.client.close()
