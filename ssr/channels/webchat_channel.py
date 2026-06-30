"""Web Chat channel — reach this agent from the persist-vault front-end.

Unlike the IM channels, Web Chat has no third-party transport: it rides the
agent's **event bus** (bridged to the per-user persist-vault cloud bus by
``ssr login`` / ``SSR_BUS_URL``). persist-vault publishes user turns as
``webchat.in.<session>`` events carrying a ``reply_topic``; this channel runs a
turn and publishes the agent's ``thought``/``tool``/``token``/``done`` events
(and any ``~/outbox`` files) back on that topic, which persist-vault streams to
the browser as SSE.

Both the sandbox's built-in agent and a paired external agent can serve this
channel, so the front-end talks to either over the same "Web Chat" surface.
"""

from __future__ import annotations

import threading

from ssr.channels.base import AbstractChannel
from ssr.config import Settings


class WebChatChannel(AbstractChannel):
    name = "webchat"

    def configure(self, settings: Settings) -> None:
        # Nothing to configure interactively — pairing (ssr login) wires the bus.
        print("Web Chat 频道经云端总线工作，无需单独配置；先运行 `ssr login` 完成配对。")

    def serve(self, settings: Settings) -> None:
        from ssr.agent.core import SSRAgent

        agent = SSRAgent(settings)
        bus = agent.bus
        lock = threading.Lock()
        histories: dict[str, list] = {}

        def handle(event) -> None:
            payload = event.payload or {}
            message = payload.get("message") or ""
            reply_topic = payload.get("reply_topic")
            session = payload.get("session_id") or "webchat"
            if not message or not reply_topic:
                return
            # Run the turn on a worker thread: the listener fires on the bus
            # client's loop thread, and publishing back from there would deadlock
            # the remote-bridge forwarder.
            threading.Thread(
                target=_run_turn, args=(agent, lock, histories, session, message, reply_topic),
                daemon=True,
            ).start()

        bus.subscribe("webchat.in.**", handle, description="Web Chat inbound")
        print(f"[webchat] serving over the bus (agent {agent.agent_id}); waiting for messages…")
        # Keep the channel process alive.
        stop = threading.Event()
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass

    # AbstractChannel requires these; Web Chat replies over the bus, not here.
    def send_message(self, target: str, text: str) -> None:  # pragma: no cover
        pass

    def send_file(self, target: str, path: str, mime_type: str) -> None:  # pragma: no cover
        pass

    def send_image(self, target: str, path_or_bytes) -> None:  # pragma: no cover
        pass


def _run_turn(agent, lock, histories, session, message, reply_topic) -> None:
    bus = agent.bus
    pending = {"thought": ""}

    def obs(ev: dict) -> None:
        t = ev.get("type")
        if t == "thinking":
            if pending["thought"]:
                bus.publish(reply_topic, {"type": "thought", "text": pending["thought"]})
            pending["thought"] = ev.get("text", "") or ""
        elif t == "tool_call":
            if pending["thought"]:
                bus.publish(reply_topic, {"type": "thought", "text": pending["thought"]})
                pending["thought"] = ""
            bus.publish(reply_topic, {"type": "tool", "name": ev.get("name", "")})

    try:
        with lock:
            agent._history = histories.setdefault(session, [])
            agent.session_id = session
            agent.add_event_observer(obs)
            try:
                reply = agent.run(message)
            finally:
                agent.remove_event_observer(obs)
        bus.publish(reply_topic, {"type": "token", "text": reply})
        bus.publish(reply_topic, {"type": "done"})
    except Exception as exc:  # noqa: BLE001
        bus.publish(reply_topic, {"type": "error", "error": f"{type(exc).__name__}: {exc}"})
