"""Tests for srdb — the per-process agent debug server.

These drive the server/client round-trip against a lightweight fake agent (real
``MessageBus`` + ``SessionStore``, no model/network) so every srdb capability is
covered: agent listing, session edit, sub-agent (bus handler) listing, bus emit,
direct channel send, and in-process ``eval``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from ssr.agent.sessions import SessionStore
from ssr.bus import MessageBus
from ssr.config import Settings
from ssr.srdb import registry
from ssr.srdb.client import SrdbClient, parse_link
from ssr.srdb.server import SrdbServer


def _settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return Settings(home=home, project_dir=tmp_path)


class _Model:
    def __init__(self, mid):
        self.id = mid


class _Models:
    def get_primary(self):
        return _Model("default-gemini")

    def get_fallbacks(self):
        return [_Model("glm")]


class FakeAgent:
    """Just enough surface for srdb to introspect/control."""

    def __init__(self, settings, agent_id="agent:test01"):
        self.agent_id = agent_id
        self.settings = settings
        self.active_im_context = ("feishu", "chat_x")
        self.session_id = None
        self._history = []
        self._bus_notify_listeners: dict[str, dict] = {}
        self.mcp_tools = []
        self.models_config = _Models()
        self.bus = MessageBus(name=agent_id, source=agent_id)
        self.sessions = SessionStore(settings)
        self.terminal_contexts = {}
        self._observers: list = []

    def is_busy(self):
        return False

    # live-event surface (mirrors SSRAgent.add_event_observer / _emit)
    def add_event_observer(self, cb):
        if cb not in self._observers:
            self._observers.append(cb)

    def remove_event_observer(self, cb):
        if cb in self._observers:
            self._observers.remove(cb)

    def emit(self, kind, **fields):
        for cb in list(self._observers):
            cb({"type": kind, "agent": "ssr", **fields})

    def load_session(self, sid):
        return True

    # bus-handler ("sub-agent") surface
    def bus_handlers(self):
        return [{"id": hid, **meta} for hid, meta in self._bus_notify_listeners.items()]

    def create_bus_handler(self, pattern, prompt="", *, once=False, inherit_session=True, description=""):
        hid = self.bus.subscribe(pattern, lambda ev: None, description=description)
        self._bus_notify_listeners[hid] = {
            "pattern": pattern, "prompt": prompt, "once": once,
            "inherit_session": inherit_session, "description": description,
        }
        return hid

    def remove_bus_handler(self, hid):
        self._bus_notify_listeners.pop(hid, None)
        return self.bus.unsubscribe(hid)


def _start_server(settings) -> SrdbServer:
    server = SrdbServer(settings, host="127.0.0.1", port=0, key="testkey")
    ready = threading.Event()

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server._loop = loop
        loop.run_until_complete(server._serve(ready))

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(5.0), "srdb server did not start"
    return server


@pytest.fixture
def agent_and_client(tmp_path):
    settings = _settings(tmp_path)
    agent = FakeAgent(settings)
    registry.register_agent(agent)
    server = _start_server(settings)
    client = SrdbClient(server.link(agent.agent_id)).connect()
    try:
        yield agent, client, server
    finally:
        client.close()
        registry.unregister_agent(agent)


# --------------------------------------------------------------------- tests
def test_parse_link_roundtrip():
    host, port, key, agent = parse_link("tcp://127.0.0.1:5005?key=abc&agent=agent:1")
    assert (host, port, key, agent) == ("127.0.0.1", 5005, "abc", "agent:1")


def test_auth_required(tmp_path):
    settings = _settings(tmp_path)
    server = _start_server(settings)
    # Wrong key → auth fails.
    import socket
    from ssr.bus import jsonrpc

    s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    s.recv(4096)  # welcome
    s.sendall((jsonrpc.dumps(jsonrpc.request("srdb.auth", {"key": "WRONG"}, 1)) + "\n").encode())
    resp = jsonrpc.loads(s.recv(4096).decode().splitlines()[0])
    assert "error" in resp and resp["error"]["code"] == jsonrpc.UNAUTHORIZED
    s.close()


def test_ping_works_without_auth(tmp_path):
    settings = _settings(tmp_path)
    server = _start_server(settings)
    import socket
    from ssr.bus import jsonrpc

    s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    s.recv(4096)
    s.sendall((jsonrpc.dumps(jsonrpc.request("srdb.ping", {}, 1)) + "\n").encode())
    resp = jsonrpc.loads(s.recv(4096).decode().splitlines()[0])
    assert resp["result"]["pong"] is True
    s.close()


def test_agents_and_agent_detail(agent_and_client):
    agent, client, server = agent_and_client
    agents = client.agents()
    assert any(a["agent_id"] == agent.agent_id for a in agents)
    detail = client.agent()
    assert detail["agent_id"] == agent.agent_id
    assert detail["model_primary"] == "default-gemini"
    assert detail["model_fallbacks"] == ["glm"]
    assert detail["active_channel"] == ["feishu", "chat_x"]


def test_session_list_get_edit(agent_and_client):
    agent, client, server = agent_and_client
    sid = agent.sessions.create(title="orig")
    agent.sessions.append(sid, "user", "hello")
    listed = client.call("srdb.sessions")["sessions"]
    assert any(s["id"] == sid for s in listed)
    # Edit: replace turns + title.
    out = client.call("srdb.session.edit", {
        "id": sid, "title": "edited",
        "turns": [{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"}],
    })
    assert out["ok"] is True
    got = client.call("srdb.session.get", {"id": sid})
    assert got["title"] == "edited"
    assert [t["content"] for t in got["turns"]] == ["Q", "A"]


def test_subagents_list_and_edit(agent_and_client):
    agent, client, server = agent_and_client
    hid = agent.create_bus_handler("task.*", "do it", description="d")
    subs = client.call("srdb.subagents")["subagents"]
    assert any(s["id"] == hid for s in subs)
    edited = client.call("srdb.subagent.edit", {"id": hid, "prompt": "new prompt", "once": True})
    assert edited["ok"] is True and edited["old_id"] == hid and edited["id"] != hid
    assert edited["config"]["prompt"] == "new prompt" and edited["config"]["once"] is True


def test_bus_state_emit_history(agent_and_client):
    agent, client, server = agent_and_client
    state = client.call("srdb.bus")
    assert state["name"] == agent.agent_id
    client.call("srdb.bus.emit", {"topic": "demo.hello", "payload": {"x": 1}})
    hist = client.call("srdb.bus.history", {"pattern": "demo.*"})["events"]
    assert any(e["topic"] == "demo.hello" and e["payload"] == {"x": 1} for e in hist)


def test_channel_send(agent_and_client):
    agent, client, server = agent_and_client
    from ssr.channels.registry import registry as chan_registry

    sent = {}

    class FakeChannel:
        name = "feishu"

        def send_message(self, target, text):
            sent["target"], sent["text"] = target, text

    ch = FakeChannel()
    chan_registry.register(ch)
    try:
        chans = client.call("srdb.channels")["channels"]
        assert any(c["name"] == "feishu" for c in chans)
        client.channel_send("feishu", "chat_42", "hi there")
        assert sent == {"target": "chat_42", "text": "hi there"}
    finally:
        chan_registry.channels.pop("feishu", None)


def test_eval_expression_statements_and_stdout(agent_and_client):
    agent, client, server = agent_and_client
    assert client.eval("1 + 2")["result"] == "3"
    out = client.eval("print('hi from agent')")
    assert "hi from agent" in out["stdout"]
    got = client.eval("_ = agent.agent_id")
    assert got["result"] == repr(agent.agent_id)
    err = client.eval("1/0")
    assert "ZeroDivisionError" in (err["error"] or "")


def test_watch_streams_live_events(agent_and_client):
    import time

    agent, client, server = agent_and_client
    watcher = SrdbClient(server.link(agent.agent_id)).connect()
    got: list = []

    def run():
        for frame in watcher.stream():
            got.append(frame)
            break  # one event proves the stream works

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Emit until the watcher (subscribed asynchronously) captures one event.
    deadline = time.time() + 3.0
    while not got and time.time() < deadline:
        agent.emit("tool_call", name="run_command", args="ls")
        time.sleep(0.05)
    t.join(timeout=3.0)
    watcher.close()
    assert got, "no live event was streamed"
    assert got[0]["agent_id"] == agent.agent_id
    assert got[0]["event"]["type"] == "tool_call"
    assert got[0]["event"]["name"] == "run_command"


def test_watch_survives_idle_periods(agent_and_client):
    # A quiet stream (longer than the client's 1s recv timeout) must not raise
    # TimeoutError; a later event must still arrive.
    import time

    agent, client, server = agent_and_client
    watcher = SrdbClient(server.link(agent.agent_id)).connect()
    got: list = []
    err: list = []

    def run():
        try:
            for frame in watcher.stream():
                got.append(frame)
                break
        except Exception as e:  # a timeout here would be the bug
            err.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(1.6)  # idle past the recv timeout
    assert not err, f"stream crashed while idle: {err}"
    deadline = time.time() + 3.0
    while not got and time.time() < deadline:
        agent.emit("tool_result", name="run_command")
        time.sleep(0.05)
    watcher.close()
    assert got and got[0]["event"]["type"] == "tool_result"
    assert not err


def test_watch_then_disconnect_detaches_observer(agent_and_client):
    agent, client, server = agent_and_client
    watcher = SrdbClient(server.link(agent.agent_id)).connect()
    watcher.call("srdb.watch", {"agent": agent.agent_id})
    assert len(agent._observers) == 1
    watcher.close()
    # The server detaches the observer when the socket drops.
    import time

    deadline = time.time() + 3.0
    while agent._observers and time.time() < deadline:
        time.sleep(0.05)
    assert agent._observers == []


def test_run_parts_emits_turn_start_and_reply(tmp_path, monkeypatch):
    # A plain (no-tool) turn must still produce live events, else `srdb watch`
    # shows nothing for a simple channel Q&A.
    monkeypatch.setenv("SSR_SRDB", "0")  # no server needed for this check
    settings = _settings(tmp_path)
    from ssr.agent.core import SSRAgent

    agent = SSRAgent(settings)
    monkeypatch.setattr(agent, "_complete", lambda *a, **k: "hi there")
    events: list = []
    agent.add_event_observer(lambda e: events.append(e))
    reply = agent.run_parts([{"type": "text", "text": "hello"}])
    assert reply == "hi there"
    kinds = [e["type"] for e in events]
    assert kinds[0] == "turn_start"
    assert "reply" in kinds
    assert events[kinds.index("reply")]["text"] == "hi there"


def test_unknown_method_errors(agent_and_client):
    agent, client, server = agent_and_client
    from ssr.srdb.client import SrdbError

    with pytest.raises(SrdbError):
        client.call("srdb.nope")
