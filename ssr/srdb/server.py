"""srdb — the SSR agent debug server.

Every SSR **main-agent process** opens one srdb server: a small TCP server that
lets a debugger inspect and steer every agent running in the process. It speaks
**newline-delimited JSON-RPC 2.0** over raw TCP (so a connection URL is a plain
``tcp://host:port?key=…``), and each live agent gets its own debug link
(``tcp://host:port?key=…&agent=<agent_id>``).

Capabilities (all gated behind the per-server ``key``):

* **inspect** — list running agents / sub-agents (bus-handler agents), detailed
  sessions, channels and bus state.
* **edit** — rewrite a recorded session, reconfigure a sub-agent (bus handler),
  inject/emit bus events.
* **act** — send a message straight to a channel, and **evaluate arbitrary
  Python** in the live process with the agent in scope.
* **watch** — stream an agent's (or sub-agent's) real-time activity:
  ``srdb.watch`` subscribes a connection and the server pushes ``srdb.event``
  notifications for every ``thinking`` / ``tool_call`` / ``tool_result`` /
  ``sub_agent`` / ``bus_event`` the agent emits.

Security: the server binds to ``127.0.0.1`` by default and every method requires
the key (presented once via ``srdb.auth``). Because ``srdb.eval`` runs arbitrary
code in-process, treat the key like a password and do not bind it to a public
interface. Disable entirely with ``SSR_SRDB=0``; override host/port/key with
``SSR_SRDB_HOST`` / ``SSR_SRDB_PORT`` / ``SSR_SRDB_KEY``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import secrets
import sys
import threading
import time
import traceback

from ..bus import jsonrpc
from . import registry


def _enabled() -> bool:
    return os.environ.get("SSR_SRDB", "1").lower() not in ("0", "false", "no", "off")


class _Conn:
    def __init__(self, authenticated: bool):
        self.authenticated = authenticated
        self.writer: asyncio.StreamWriter | None = None
        # Live watchers registered on agents: [(agent, callback), …] — removed
        # on disconnect so a dropped debugger never leaks observers.
        self.observers: list = []


class SrdbServer:
    """Per-process TCP debug server over live :class:`SSRAgent` instances."""

    def __init__(self, settings, host: str = "127.0.0.1", port: int = 0, key: str | None = None):
        self.settings = settings
        self.host = host
        self.port = port            # 0 → OS allocates; real port set after bind
        self.key = key or secrets.token_hex(16)
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------- link
    def link(self, agent_id: str | None = None) -> str:
        q = f"key={self.key}"
        if agent_id:
            q += f"&agent={agent_id}"
        return f"tcp://{self.host}:{self.port}?{q}"

    # ---------------------------------------------------------------- serving
    async def _serve(self, ready: "threading.Event | None" = None) -> None:
        server = await asyncio.start_server(self._handle, self.host, self.port)
        # When port was 0, capture the OS-allocated port so links are accurate.
        self.port = server.sockets[0].getsockname()[1]
        if ready is not None:
            ready.set()
        async with server:
            await server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = _Conn(authenticated=False)
        conn.writer = writer
        await self._send(writer, jsonrpc.notification("srdb.welcome", {"requires_auth": True}))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                raw = line.decode("utf-8", "replace").strip()
                if raw:
                    await self._on_message(conn, writer, raw)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            pass
        finally:
            # Detach any live watchers this connection registered.
            for agent, cb in list(conn.observers):
                with contextlib.suppress(Exception):
                    agent.remove_event_observer(cb)
            conn.observers.clear()
            with contextlib.suppress(Exception):
                writer.close()

    async def _on_message(self, conn: _Conn, writer: asyncio.StreamWriter, raw: str) -> None:
        try:
            msg = jsonrpc.loads(raw)
        except Exception:
            await self._send(writer, jsonrpc.error(None, jsonrpc.PARSE_ERROR, "invalid JSON"))
            return
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if not method:
            return
        try:
            # Dispatch off the event loop: introspection touches agent state and
            # `srdb.eval` may block, so never freeze the server's loop.
            value = await asyncio.get_running_loop().run_in_executor(
                None, self._dispatch, conn, method, params
            )
            if mid is not None:
                await self._send(writer, jsonrpc.result(mid, value))
        except jsonrpc.JsonRpcError as e:
            if mid is not None:
                await self._send(writer, jsonrpc.error(mid, e.code, e.message, e.data))
        except Exception as e:  # pragma: no cover - defensive
            if mid is not None:
                await self._send(writer, jsonrpc.error(mid, jsonrpc.INTERNAL_ERROR, str(e)))

    async def _send(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        try:
            writer.write((jsonrpc.dumps(obj) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass

    # --------------------------------------------------------------- dispatch
    def _dispatch(self, conn: _Conn, method: str, params: dict):
        if method == "srdb.auth":
            if str(params.get("key") or "") == self.key:
                conn.authenticated = True
                return {"ok": True}
            raise jsonrpc.JsonRpcError(jsonrpc.UNAUTHORIZED, "invalid srdb key")
        if method == "srdb.ping":
            return {"pong": True, "agents": len(registry.list_agents()), "ts": time.time()}
        if not conn.authenticated:
            raise jsonrpc.JsonRpcError(
                jsonrpc.UNAUTHORIZED, "authentication required: call srdb.auth first"
            )

        # Streaming subscribe/unsubscribe need the connection itself.
        if method == "srdb.watch":
            return self._watch(conn, params)
        if method == "srdb.unwatch":
            return self._unwatch(conn, params)

        handler = _METHODS.get(method)
        if handler is None:
            raise jsonrpc.JsonRpcError(jsonrpc.METHOD_NOT_FOUND, f"unknown method: {method}")
        return handler(self, params)

    # ----------------------------------------------------------- live watching
    def _watch(self, conn: _Conn, params: dict) -> dict:
        """Stream an agent's live events to this connection as ``srdb.event``.

        ``agent`` selects one agent (default: any), or ``"all"``/``"*"`` to watch
        every agent currently running in the process.
        """
        target = params.get("agent")
        if target in ("all", "*"):
            agents = registry.list_agents()
        elif target:
            a = registry.get_agent(target)
            if a is None:
                raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, f"no such agent: {target}")
            agents = [a]
        else:
            a = registry.first_agent()
            agents = [a] if a else []
        if not agents:
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, "no agents to watch")
        watching = []
        for agent in agents:
            if not hasattr(agent, "add_event_observer"):
                continue
            cb = self._make_forwarder(conn, agent)
            agent.add_event_observer(cb)
            conn.observers.append((agent, cb))
            watching.append(agent.agent_id)
        return {"ok": True, "watching": watching}

    def _unwatch(self, conn: _Conn, params: dict) -> dict:
        n = 0
        for agent, cb in list(conn.observers):
            with contextlib.suppress(Exception):
                agent.remove_event_observer(cb)
                n += 1
        conn.observers.clear()
        return {"ok": True, "removed": n}

    def _make_forwarder(self, conn: _Conn, agent):
        """An observer that ships ``agent``'s events to ``conn`` over the loop.

        The observer runs on the *agent's* thread, so it hands the write back to
        the server's event loop with ``call_soon_threadsafe``.
        """
        loop = self._loop

        def forward(event: dict) -> None:
            if loop is None or conn.writer is None:
                return
            note = jsonrpc.notification(
                "srdb.event", {"agent_id": agent.agent_id, "event": event}
            )
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._send(conn.writer, note))
                )

        return forward

    # ------------------------------------------------------------- resolvers
    def _resolve_agent(self, params: dict):
        aid = params.get("agent")
        agent = registry.get_agent(aid) if aid else registry.first_agent()
        if agent is None:
            raise jsonrpc.JsonRpcError(
                jsonrpc.INVALID_PARAMS,
                f"no such agent: {aid!r}" if aid else "no agents are running",
            )
        return agent

    # ============================================================== inspect
    def m_agents(self, params: dict) -> dict:
        return {"agents": [_agent_summary(a) for a in registry.list_agents()]}

    def m_agent(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        info = _agent_summary(agent)
        info.update(
            {
                "link": self.link(agent.agent_id),
                "model_fallbacks": [e.id for e in agent.models_config.get_fallbacks()],
                "project_dir": str(getattr(agent.settings, "project_dir", "")),
                "terminals": list(getattr(agent, "terminal_contexts", {}) or {}),
                "mcp_servers": sorted({t.get("server") for t in (getattr(agent, "mcp_tools", []) or []) if isinstance(t, dict) and t.get("server")}),
            }
        )
        return info

    # ============================================================== sessions
    def m_sessions(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        return {"active": agent.session_id, "sessions": agent.sessions.list()}

    def m_session_get(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        sid = params.get("id") or agent.session_id
        if not sid:
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, "id is required (no active session)")
        data = agent.sessions.get(sid)
        if data is None:
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, f"no such session: {sid}")
        return data

    def m_session_edit(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        sid = params.get("id") or agent.session_id
        if not sid:
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, "id is required")
        ok = agent.sessions.replace(
            sid,
            turns=params.get("turns"),
            title=params.get("title"),
            meta_updates=params.get("meta"),
        )
        if not ok:
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, f"no such session: {sid}")
        # If the live agent is currently on this session, reload its history so
        # the edit takes effect on the next turn.
        if agent.session_id == sid:
            with contextlib.suppress(Exception):
                agent.load_session(sid)
        return {"ok": True, "session": agent.sessions.get(sid)}

    # ============================================================== subagents
    def m_subagents(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        # The persistent "sub-agents" are bus-handler agents: each fires a fresh
        # agent turn on every matching event.
        return {"subagents": agent.bus_handlers()}

    def m_subagent_edit(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        hid = params.get("id")
        handlers = {h["id"]: h for h in agent.bus_handlers()}
        if hid not in handlers:
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, f"no such sub-agent: {hid}")
        cur = handlers[hid]
        merged = {
            "pattern": params.get("pattern", cur.get("pattern")),
            "prompt": params.get("prompt", cur.get("prompt", "")),
            "once": bool(params.get("once", cur.get("once", False))),
            "inherit_session": bool(params.get("inherit_session", cur.get("inherit_session", True))),
            "description": params.get("description", cur.get("description", "")),
        }
        # The handler closure captures its config by value, so a real edit means
        # re-registering — the id changes; return the new one.
        agent.remove_bus_handler(hid)
        new_id = agent.create_bus_handler(
            merged["pattern"],
            merged["prompt"],
            once=merged["once"],
            inherit_session=merged["inherit_session"],
            description=merged["description"],
        )
        return {"ok": True, "old_id": hid, "id": new_id, "config": merged}

    def m_subagent_remove(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        return {"ok": bool(agent.remove_bus_handler(params.get("id")))}

    # ============================================================== channels
    def m_channels(self, params: dict) -> dict:
        from ssr.channels.registry import registry as chan_registry

        out = []
        for ch in chan_registry.list_channels():
            out.append(
                {
                    "name": ch.name,
                    "class": type(ch).__name__,
                    "state": _channel_state(ch),
                }
            )
        return {"channels": out}

    def m_channel_send(self, params: dict) -> dict:
        from ssr.channels.registry import registry as chan_registry

        name = params.get("channel")
        target = params.get("target") or ""
        text = params.get("text") or ""
        ch = chan_registry.get(name) if name else None
        if ch is None:
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, f"no such channel: {name!r}")
        try:
            ch.send_message(target, text)
        except Exception as e:
            raise jsonrpc.JsonRpcError(jsonrpc.INTERNAL_ERROR, f"send failed: {e}")
        return {"ok": True}

    # ============================================================== bus
    def m_bus(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        return {
            "agent_id": agent.agent_id,
            "name": agent.bus.name,
            "bridged": agent.bus.bridged,  # property
            "listeners": agent.bus.listeners(),
        }

    def m_bus_history(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        events = agent.bus.history(str(params.get("pattern") or "**"), int(params.get("limit") or 50))
        return {"events": [e.to_dict() for e in events]}

    def m_bus_emit(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        topic = params.get("topic")
        if not topic:
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, "topic is required")
        event = agent.bus.publish(
            str(topic), params.get("payload") or {}, source=str(params.get("source") or "srdb")
        )
        return {"event": event.to_dict()}

    # ============================================================== eval
    def m_eval(self, params: dict) -> dict:
        agent = self._resolve_agent(params)
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, "code (string) is required")
        ns = {
            "agent": agent,
            "settings": agent.settings,
            "registry": registry,
            "bus": agent.bus,
            "__name__": "__srdb__",
        }
        buf = io.StringIO()
        result_repr = None
        error = None
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                try:
                    # Expression? eval it and capture the value.
                    value = eval(compile(code, "<srdb>", "eval"), ns, ns)
                    result_repr = repr(value)
                except SyntaxError:
                    # Statements: exec, then surface a trailing `_` if set.
                    exec(compile(code, "<srdb>", "exec"), ns, ns)
                    if "_" in ns:
                        result_repr = repr(ns["_"])
            except Exception:
                error = traceback.format_exc()
        return {"stdout": buf.getvalue(), "result": result_repr, "error": error}


_METHODS = {
    "srdb.agents": SrdbServer.m_agents,
    "srdb.agent": SrdbServer.m_agent,
    "srdb.sessions": SrdbServer.m_sessions,
    "srdb.session.get": SrdbServer.m_session_get,
    "srdb.session.edit": SrdbServer.m_session_edit,
    "srdb.subagents": SrdbServer.m_subagents,
    "srdb.subagent.edit": SrdbServer.m_subagent_edit,
    "srdb.subagent.remove": SrdbServer.m_subagent_remove,
    "srdb.channels": SrdbServer.m_channels,
    "srdb.channel.send": SrdbServer.m_channel_send,
    "srdb.bus": SrdbServer.m_bus,
    "srdb.bus.history": SrdbServer.m_bus_history,
    "srdb.bus.emit": SrdbServer.m_bus_emit,
    "srdb.eval": SrdbServer.m_eval,
}


# --------------------------------------------------------------- serializers
def _agent_summary(agent) -> dict:
    return {
        "agent_id": agent.agent_id,
        "active_channel": list(agent.active_im_context) if getattr(agent, "active_im_context", None) else None,
        "session_id": agent.session_id,
        "busy": agent.is_busy() if hasattr(agent, "is_busy") else None,
        "history_turns": len(getattr(agent, "_history", []) or []),
        "bus_handlers": len(getattr(agent, "_bus_notify_listeners", {}) or {}),
        "mcp_tools": len(getattr(agent, "mcp_tools", []) or []),
        "model_primary": _safe(lambda: agent.models_config.get_primary().id),
        "bus_bridged": _safe(lambda: agent.bus.bridged),
    }


def _channel_state(ch) -> dict:
    """Best-effort snapshot of a channel's public-ish runtime state."""
    keys = ("name", "default_cwd", "uin", "bot_token", "_speaker", "_loop", "api")
    state: dict = {}
    for k in keys:
        if hasattr(ch, k):
            v = getattr(ch, k)
            if k in ("bot_token",) and v:
                v = "***"  # don't leak credentials over the wire
            state[k] = bool(v) if k in ("_speaker", "_loop", "api") else v
    return state


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


# ------------------------------------------------------------- process singleton
_SERVER: SrdbServer | None = None
_SERVER_LOCK = threading.Lock()


def ensure_server(settings) -> "SrdbServer | None":
    """Start (once) and return this process's srdb server, or None if disabled.

    Idempotent and best-effort: a failure to bind never breaks agent startup.
    """
    global _SERVER
    if not _enabled():
        return None
    if _SERVER is not None:
        return _SERVER
    with _SERVER_LOCK:
        if _SERVER is not None:
            return _SERVER
        host = os.environ.get("SSR_SRDB_HOST", "127.0.0.1")
        try:
            port = int(os.environ.get("SSR_SRDB_PORT", "0"))
        except ValueError:
            port = 0
        key = os.environ.get("SSR_SRDB_KEY") or None
        server = SrdbServer(settings, host=host, port=port, key=key)
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            server._loop = loop
            try:
                loop.run_until_complete(server._serve(ready))
            except Exception:
                ready.set()
            finally:
                with contextlib.suppress(Exception):
                    loop.close()

        threading.Thread(target=_run, daemon=True, name="ssr-srdb").start()
        if not ready.wait(5.0):
            return None
        _SERVER = server
        return _SERVER


def announce_link(link: str) -> None:
    """Print an agent's srdb link to stderr (safe for ACP's clean stdout)."""
    try:
        print(f"[srdb] debug this agent: {link}", file=sys.stderr, flush=True)
    except Exception:
        pass
