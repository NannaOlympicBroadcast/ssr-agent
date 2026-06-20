"""The remote bus server.

A standalone WebSocket server that brokers :class:`BusEvent`s between any number
of connected peers (SSR agents, external programs, other AI agents). Peers speak
structured JSON-RPC 2.0 over the socket.

Client → server methods:

* ``bus.publish``    params ``{topic, payload?, source?, correlation_id?}`` → ``{event}``
* ``bus.subscribe``  params ``{pattern}`` → ``{subscription_id}``
* ``bus.unsubscribe``params ``{subscription_id}`` → ``{ok}``
* ``bus.subscriptions`` → ``{subscriptions: [...]}``
* ``bus.history``    params ``{pattern?, limit?}`` → ``{events: [...]}``
* ``bus.ping``       → ``{pong: true, peers, ts}``

Server → client notifications:

* ``bus.event``      params ``{event}`` — pushed for every event matching one of
  the peer's subscriptions.

Run with ``ssr bus serve``.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from . import jsonrpc
from .core import MessageBus
from .events import BusEvent, topic_matches


class _Peer:
    def __init__(self, ws, authenticated: bool = True):
        self.ws = ws
        self.id = uuid.uuid4().hex[:12]
        self.subscriptions: dict[str, str] = {}  # subscription_id -> pattern
        # When the server requires an api key a peer starts unauthenticated and
        # may only call ``bus.auth`` until it presents the right key.
        self.authenticated = authenticated


class BusServer:
    """Brokers events between connected WebSocket peers.

    When ``api_key`` is set, every peer must authenticate (call ``bus.auth``
    with the matching key) before any other method is accepted.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        name: str = "ssr-bus",
        api_key: str | None = None,
    ):
        self.host = host
        self.port = port
        self.api_key = api_key or None
        self.bus = MessageBus(name=name, source=name)
        self._peers: dict[str, _Peer] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # --------------------------------------------------------------- serving
    async def serve(self) -> None:
        import websockets

        self._loop = asyncio.get_event_loop()
        async with websockets.serve(self._handler, self.host, self.port, max_size=8 * 1024 * 1024):
            print(f"[ssr-bus] listening on ws://{self.host}:{self.port}")
            await asyncio.Future()  # run forever

    async def _handler(self, ws, *_args) -> None:
        peer = _Peer(ws, authenticated=self.api_key is None)
        self._peers[peer.id] = peer
        try:
            await self._send(
                ws,
                jsonrpc.notification(
                    "bus.welcome",
                    {"peer_id": peer.id, "requires_auth": self.api_key is not None},
                ),
            )
            async for raw in ws:
                await self._on_message(peer, raw)
        except Exception:
            pass
        finally:
            self._peers.pop(peer.id, None)

    async def _on_message(self, peer: _Peer, raw: str) -> None:
        try:
            msg = jsonrpc.loads(raw)
        except Exception:
            await self._send(peer.ws, jsonrpc.error(None, jsonrpc.PARSE_ERROR, "invalid JSON"))
            return
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if not method:
            return  # responses/acks are not expected by the server
        try:
            value = await self._dispatch(peer, method, params)
            if mid is not None:
                await self._send(peer.ws, jsonrpc.result(mid, value))
        except jsonrpc.JsonRpcError as e:
            if mid is not None:
                await self._send(peer.ws, jsonrpc.error(mid, e.code, e.message, e.data))
        except Exception as e:  # pragma: no cover - defensive
            if mid is not None:
                await self._send(peer.ws, jsonrpc.error(mid, jsonrpc.INTERNAL_ERROR, str(e)))

    async def _dispatch(self, peer: _Peer, method: str, params: dict):
        if method == "bus.auth":
            if self.api_key is None:
                peer.authenticated = True
                return {"ok": True, "required": False}
            if str(params.get("key") or "") == self.api_key:
                peer.authenticated = True
                return {"ok": True, "required": True}
            raise jsonrpc.JsonRpcError(jsonrpc.UNAUTHORIZED, "invalid bus api key")

        # Gate every other method behind authentication when a key is required.
        if not peer.authenticated:
            raise jsonrpc.JsonRpcError(
                jsonrpc.UNAUTHORIZED, "authentication required: call bus.auth first"
            )

        if method == "bus.publish":
            topic = params.get("topic")
            if not topic:
                raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, "topic is required")
            event = BusEvent.from_dict(
                {
                    "topic": str(topic),
                    "payload": params.get("payload") or {},
                    "source": str(params.get("source") or peer.id),
                    "correlation_id": params.get("correlation_id"),
                    # Preserve the originating id when a bridge supplies one so
                    # the echoed copy de-duplicates on the publisher's local bus.
                    "id": params.get("id"),
                }
            )
            self.bus.emit(event, forward=False)
            await self._broadcast(event)
            return {"event": event.to_dict()}

        if method == "bus.subscribe":
            sub_id = uuid.uuid4().hex[:12]
            peer.subscriptions[sub_id] = str(params.get("pattern") or "**")
            return {"subscription_id": sub_id}

        if method == "bus.unsubscribe":
            ok = peer.subscriptions.pop(str(params.get("subscription_id")), None) is not None
            return {"ok": ok}

        if method == "bus.subscriptions":
            return {"subscriptions": [{"id": k, "pattern": v} for k, v in peer.subscriptions.items()]}

        if method == "bus.history":
            events = self.bus.history(str(params.get("pattern") or "**"), int(params.get("limit") or 50))
            return {"events": [e.to_dict() for e in events]}

        if method == "bus.ping":
            return {"pong": True, "peers": len(self._peers), "ts": time.time()}

        raise jsonrpc.JsonRpcError(jsonrpc.METHOD_NOT_FOUND, f"unknown method: {method}")

    async def _broadcast(self, event: BusEvent) -> None:
        """Push the event to every peer with a matching subscription."""
        note = jsonrpc.notification("bus.event", {"event": event.to_dict()})
        for peer in list(self._peers.values()):
            if any(topic_matches(p, event.topic) for p in peer.subscriptions.values()):
                await self._send(peer.ws, note)

    async def _send(self, ws, obj: dict) -> None:
        try:
            await ws.send(jsonrpc.dumps(obj))
        except Exception:
            pass


def run_server(host: str = "127.0.0.1", port: int = 8765, api_key: str | None = None) -> None:
    server = BusServer(host=host, port=port, api_key=api_key)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        print("\n[ssr-bus] shutting down")


def serve_in_thread(
    host: str = "127.0.0.1",
    port: int = 8765,
    api_key: str | None = None,
    name: str = "ssr-bus",
    ready_timeout: float = 5.0,
) -> tuple["BusServer", "threading.Thread"]:
    """Start a :class:`BusServer` on a daemon thread and return once it is bound.

    Non-blocking: the websocket server runs on its own asyncio loop in a
    background thread so the caller (an SSR main process) keeps running. Raises
    the bind error (e.g. :class:`OSError` when the port is already in use) so the
    caller can decide to reuse an existing server instead.
    """
    import threading

    server = BusServer(host=host, port=port, name=name, api_key=api_key)
    ready = threading.Event()
    box: dict[str, BaseException] = {}

    def _run() -> None:
        import websockets

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server._loop = loop

        async def _serve() -> None:
            try:
                ws_server = await websockets.serve(
                    server._handler, host, port, max_size=8 * 1024 * 1024
                )
            except BaseException as e:  # bind failure, etc.
                box["error"] = e
                ready.set()
                return
            ready.set()
            try:
                async with ws_server:
                    await asyncio.Future()  # run forever
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                pass

        try:
            loop.run_until_complete(_serve())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name=name)
    thread.start()
    if not ready.wait(ready_timeout):
        raise TimeoutError(f"bus server failed to start on {host}:{port}")
    if "error" in box:
        raise box["error"]
    return server, thread
