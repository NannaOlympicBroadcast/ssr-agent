"""Tests for the SSR event bus: events, in-process bus, and the server/client.

The server/client tests spin up a real WebSocket bus server on a free localhost
port (no mocks) and drive it through the synchronous ``BusClient`` facade.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

from ssr.bus import BusClient, BusServer, MessageBus, RemoteBusBridge
from ssr.bus.events import BusEvent, topic_matches


# ------------------------------------------------------------------ events
def test_topic_matches_wildcards():
    assert topic_matches("task.done", "task.done")
    assert not topic_matches("task.done", "task.failed")
    assert topic_matches("task.*", "task.done")
    assert not topic_matches("task.*", "task.done.extra")
    assert topic_matches("task.**", "task.done.extra")
    assert topic_matches("**", "anything.at.all")
    assert topic_matches("*", "anything")
    assert topic_matches("agent.*.done", "agent.alice.done")
    assert not topic_matches("agent.*.done", "agent.alice.failed")


def test_busevent_roundtrip():
    ev = BusEvent(topic="x.y", payload={"a": 1}, source="me")
    d = ev.to_dict()
    ev2 = BusEvent.from_dict(d)
    assert ev2.topic == "x.y"
    assert ev2.payload == {"a": 1}
    assert ev2.id == ev.id
    # non-dict payloads are wrapped
    assert BusEvent.from_dict({"topic": "t", "payload": 5}).payload == {"value": 5}


# ------------------------------------------------------------- MessageBus
def test_messagebus_pubsub_and_wildcards():
    bus = MessageBus(name="t")
    received = []
    bus.subscribe("task.*", received.append)
    bus.publish("task.done", {"id": 1})
    bus.publish("other.thing", {"id": 2})
    assert len(received) == 1
    assert received[0].topic == "task.done"


def test_messagebus_once_and_unsubscribe():
    bus = MessageBus()
    hits = []
    bus.subscribe("a", hits.append, once=True)
    bus.publish("a")
    bus.publish("a")
    assert len(hits) == 1

    other = []
    lid = bus.subscribe("b", other.append)
    assert bus.unsubscribe(lid)
    bus.publish("b")
    assert other == []


def test_messagebus_wait_for():
    bus = MessageBus()

    def _later():
        time.sleep(0.2)
        bus.publish("ready", {"ok": True})

    threading.Thread(target=_later, daemon=True).start()
    ev = bus.wait_for("ready", timeout=2.0)
    assert ev is not None and ev.payload == {"ok": True}

    # timeout path
    assert bus.wait_for("never", timeout=0.2) is None


def test_messagebus_dedup_by_id():
    bus = MessageBus()
    hits = []
    bus.subscribe("**", hits.append)
    ev = BusEvent(topic="dup", id="fixed-id")
    bus.emit(ev)
    bus.emit(BusEvent(topic="dup", id="fixed-id"))  # same id -> dropped
    assert len(hits) == 1


def test_messagebus_history():
    bus = MessageBus()
    bus.publish("a.1")
    bus.publish("a.2")
    bus.publish("b.1")
    topics = [e.topic for e in bus.history("a.*", 10)]
    assert topics == ["a.1", "a.2"]


# --------------------------------------------------------- server + client
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def bus_server():
    port = _free_port()
    server = BusServer(host="127.0.0.1", port=port)
    ready = threading.Event()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _serve():
            import websockets

            async with websockets.serve(server._handler, server.host, server.port):
                ready.set()
                await asyncio.Future()

        try:
            loop.run_until_complete(_serve())
        except Exception:
            ready.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(5), "bus server did not start"
    time.sleep(0.1)
    yield f"ws://127.0.0.1:{port}"


def test_client_publish_subscribe(bus_server):
    url = bus_server
    sub = BusClient(url, source="subscriber").connect()
    received = []
    done = threading.Event()

    def _on(ev):
        received.append(ev)
        done.set()

    sub.subscribe("task.*", _on)
    time.sleep(0.1)

    pub = BusClient(url, source="publisher").connect()
    pub.publish("task.done", {"n": 7})

    assert done.wait(3), "did not receive event"
    assert received[0].topic == "task.done"
    assert received[0].payload == {"n": 7}

    # ping + history
    assert sub.ping().get("pong") is True
    hist = pub.history("task.*", 10)
    assert any(e.topic == "task.done" for e in hist)

    sub.close()
    pub.close()


def test_client_wait_for(bus_server):
    url = bus_server
    waiter = BusClient(url, source="waiter").connect()
    pub = BusClient(url, source="pub").connect()

    def _later():
        time.sleep(0.3)
        pub.publish("signal.go", {"v": 1})

    threading.Thread(target=_later, daemon=True).start()
    ev = waiter.wait_for("signal.go", timeout=3.0)
    assert ev is not None and ev.payload == {"v": 1}
    waiter.close()
    pub.close()


def test_client_reconnects_after_connection_drop(bus_server):
    """A dead connection (server bounce, network blip) must not strand the
    client: RPCs made during the gap should fail fast (not hang for the ~20s
    outer timeout), and the client should transparently reconnect and replay
    its subscriptions once the network is back, with no caller intervention.
    """
    url = bus_server
    received = []
    client = BusClient(url, source="dropper").connect()
    client.subscribe("ping.*", received.append)
    time.sleep(0.1)

    # Simulate the connection dying out from under the client (it never called
    # close() itself) -- e.g. a server restart or a network blip.
    asyncio.run_coroutine_threadsafe(client._ws.close(), client._loop).result(5)

    start = time.monotonic()
    with pytest.raises(Exception):
        client.publish("ping.during_drop", {})
    assert time.monotonic() - start < 5, "RPC during a drop must fail fast, not hang"

    # The background reconnect loop (1s initial backoff) should bring the
    # client back -- including its subscription -- without the caller doing
    # anything. Retry the publish (not just the check) since each attempt is a
    # fire-and-forget broadcast: one landing before resubscription finishes
    # would otherwise be lost forever.
    other = BusClient(url, source="other").connect()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not received:
        try:
            other.publish("ping.after_reconnect", {})
        except Exception:
            pass
        time.sleep(0.3)

    assert received and received[0].topic == "ping.after_reconnect", (
        "client did not reconnect and replay its subscription"
    )

    client.close()
    other.close()


def test_remote_bridge_between_two_buses(bus_server):
    """Two independent in-process buses share events through the server."""
    url = bus_server
    bus_a = MessageBus(name="A", source="A")
    bus_b = MessageBus(name="B", source="B")
    bridge_a = RemoteBusBridge(bus_a, url).start()
    bridge_b = RemoteBusBridge(bus_b, url).start()
    time.sleep(0.2)

    got_on_b = []
    done = threading.Event()

    def _on_b(ev):
        got_on_b.append(ev)
        done.set()

    bus_b.subscribe("ping.*", _on_b)
    time.sleep(0.1)

    # Publish on A; it should arrive on B via the server, exactly once.
    bus_a.publish("ping.hello", {"from": "A"})

    assert done.wait(3), "event did not bridge from A to B"
    assert got_on_b[0].topic == "ping.hello"
    assert got_on_b[0].payload == {"from": "A"}
    # no echo loop: A should not have re-dispatched duplicates
    time.sleep(0.3)
    assert len(got_on_b) == 1

    bridge_a.stop()
    bridge_b.stop()


# ----------------------------------------------------------- toolkit tools
def _toolkit_with_bus(tmp_path):
    from ssr.agent.tools import ToolKit
    from ssr.config import Settings

    settings = Settings(home=tmp_path / "home", project_dir=tmp_path / "proj")
    settings.ensure_dirs()
    tk = ToolKit(settings, retriever=None, memory=None)

    class _StubAgent:
        """Faithful stand-in for SSRAgent's bus-handler API (records fires)."""

        def __init__(self):
            self.bus = MessageBus(name="stub", source="stub")
            self._bus_notify_listeners = {}
            self.fired = []

        def create_bus_handler(self, pattern, handler_prompt="", *, once=False,
                               inherit_session=True, description=""):
            def _on(ev):
                if once:
                    self.bus.unsubscribe(lid)
                    self._bus_notify_listeners.pop(lid, None)
                self.fired.append((ev.topic, handler_prompt, inherit_session))
            lid = self.bus.subscribe(pattern, _on, description=description)
            self._bus_notify_listeners[lid] = {
                "pattern": pattern, "prompt": handler_prompt, "once": once,
                "inherit_session": inherit_session, "description": description,
            }
            return lid

        def remove_bus_handler(self, hid):
            self._bus_notify_listeners.pop(hid, None)
            return self.bus.unsubscribe(hid)

        def bus_handlers(self):
            return [{"id": k, **v} for k, v in self._bus_notify_listeners.items()]

    tk.agent_instance = _StubAgent()
    return tk


def test_toolkit_bus_publish_history_handlers(tmp_path):
    tk = _toolkit_with_bus(tmp_path)
    assert "Published" in tk.bus_publish("task.done", '{"id": 3}')
    assert "task.done" in tk.bus_history("**", 10)
    out = tk.bus_create_handler("task.*", "watch tasks", type="every", inherit_session=False)
    assert "Created bus handler" in out and "isolated" in out
    assert "pattern='task.*'" in tk.bus_listeners()


def test_toolkit_bus_handler_fires_and_once(tmp_path):
    tk = _toolkit_with_bus(tmp_path)
    agent = tk.agent_instance
    # 'every' handler fires on each match.
    tk.bus_create_handler("done.*", "react", type="every")
    agent.bus.publish("done.a", {})
    agent.bus.publish("done.b", {})
    assert len(agent.fired) == 2
    # 'once' handler fires a single time, then auto-removes.
    agent.fired.clear()
    tk.bus_create_handler("one.*", "react once", type="once")
    agent.bus.publish("one.x", {})
    agent.bus.publish("one.y", {})
    assert len(agent.fired) == 1
    # string arg coercion: type/inherit_session may arrive as strings.
    out = tk.bus_create_handler("s.*", "p", type="once", inherit_session="false")
    hid = out.split("handler ")[1].split(" ")[0]
    assert "Removed bus handler." == tk.bus_remove_handler(hid)


def test_toolkit_bus_no_agent(tmp_path):
    from ssr.agent.tools import ToolKit
    from ssr.config import Settings

    settings = Settings(home=tmp_path / "home", project_dir=tmp_path / "proj")
    settings.ensure_dirs()
    tk = ToolKit(settings, retriever=None, memory=None)  # no agent_instance
    assert tk.bus_publish("x").startswith("ERROR")
    assert tk.bus_create_handler("x", "p").startswith("ERROR")


def test_cli_bus_parser():
    from ssr.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["bus", "send", "task.done", '{"id": 1}', "--url", "ws://x:1"])
    assert args.command == "bus" and args.bus_action == "send"
    assert args.topic == "task.done" and args.url == "ws://x:1"
    args = parser.parse_args(["bus", "serve", "--port", "9000"])
    assert args.bus_action == "serve" and args.port == 9000
