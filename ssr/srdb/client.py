"""Synchronous srdb client — connect to a ``tcp://host:port?key=…`` link.

Used by ``ssr srdb`` and by external debuggers/tests to drive a running agent
process. Speaks the same newline-delimited JSON-RPC 2.0 as :class:`SrdbServer`.
"""

from __future__ import annotations

import socket
import threading
from urllib.parse import parse_qs, urlparse

from ..bus import jsonrpc


def parse_link(link: str) -> tuple[str, int, str | None, str | None]:
    """Split ``tcp://host:port?key=…&agent=…`` into ``(host, port, key, agent)``."""
    u = urlparse(link)
    if u.scheme != "tcp" or not u.hostname or not u.port:
        raise ValueError(f"not an srdb tcp link: {link!r}")
    q = parse_qs(u.query)
    key = (q.get("key") or [None])[0]
    agent = (q.get("agent") or [None])[0]
    return u.hostname, u.port, key, agent


class SrdbClient:
    """A blocking client for one srdb connection."""

    def __init__(self, link: str, timeout: float = 10.0):
        self.host, self.port, self.key, self.agent_id = parse_link(link)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._id = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> "SrdbClient":
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._read_message()  # consume the srdb.welcome notification
        if self.key:
            self.call("srdb.auth", {"key": self.key})
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "SrdbClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------------- calls
    def call(self, method: str, params: dict | None = None) -> dict:
        if self._sock is None:
            raise RuntimeError("not connected")
        with self._lock:
            self._id += 1
            mid = self._id
            self._sock.sendall((jsonrpc.dumps(jsonrpc.request(method, params or {}, mid)) + "\n").encode("utf-8"))
            while True:
                msg = self._read_message()
                if msg.get("id") != mid:
                    continue  # skip notifications / out-of-band frames
                if "error" in msg:
                    err = msg["error"]
                    raise SrdbError(err.get("code"), err.get("message"), err.get("data"))
                return msg.get("result")

    # -------- convenience wrappers (agent defaults to the link's ?agent=) ----
    def _p(self, params: dict | None = None) -> dict:
        p = dict(params or {})
        if self.agent_id and "agent" not in p:
            p["agent"] = self.agent_id
        return p

    def agents(self) -> list:
        return self.call("srdb.agents")["agents"]

    def agent(self, agent_id: str | None = None) -> dict:
        return self.call("srdb.agent", self._p({"agent": agent_id} if agent_id else {}))

    def eval(self, code: str) -> dict:
        return self.call("srdb.eval", self._p({"code": code}))

    def channel_send(self, channel: str, target: str, text: str) -> dict:
        return self.call("srdb.channel.send", {"channel": channel, "target": target, "text": text})

    def stream(self, agent_id: str | None = None):
        """Yield live ``{agent_id, event}`` dicts for an agent (or ``"all"``).

        Subscribes via ``srdb.watch`` and then reads pushed ``srdb.event``
        notifications forever. This call takes over the connection — don't
        interleave :meth:`call` with it.
        """
        if self._sock is None:
            raise RuntimeError("not connected")
        self._id += 1
        mid = self._id
        params: dict = {}
        target = agent_id if agent_id is not None else self.agent_id
        if target:
            params["agent"] = target
        self._sock.sendall(
            (jsonrpc.dumps(jsonrpc.request("srdb.watch", params, mid)) + "\n").encode("utf-8")
        )
        # A live stream is mostly idle, so don't let the connect timeout fire on
        # quiet periods. Use a short recv timeout and loop — that also keeps
        # Ctrl-C responsive on Windows (a fully-blocking recv isn't interruptible).
        self._sock.settimeout(1.0)
        while True:
            try:
                msg = self._read_message()
            except (socket.timeout, TimeoutError):
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    err = msg["error"]
                    raise SrdbError(err.get("code"), err.get("message"), err.get("data"))
                continue  # the watch ack
            if msg.get("method") == "srdb.event":
                yield msg.get("params") or {}

    # ----------------------------------------------------------------- wire
    def _read_message(self) -> dict:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("srdb connection closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return jsonrpc.loads(line.decode("utf-8", "replace"))


class SrdbError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data
