"""Remote control client — turns this SSR instance into a dispatch *node*.

``ssr rc`` opens a single outbound WebSocket to a dispatch server
(see the companion ``ssr-dispatch-server`` project) and serves remote requests:
filesystem browsing, file/image transfer, an interactive terminal (PTY) on this
machine, one-shot shell commands, and full agent runs dispatched over MCP.

Design notes:

* The node always *dials out*, so it works behind NAT/firewalls with no inbound
  port. Authentication is the user's API token (minted on the dispatch web UI).
* The transport is the ``websockets`` library (already an optional dependency for
  ACP). Blocking work (filesystem, ``agent.run``) is offloaded with
  ``asyncio.to_thread`` so the receive loop stays responsive.
* Terminals use a real PTY on POSIX; on platforms without ``pty`` we degrade to a
  line-oriented pipe shell so the feature still works, just without full TTY
  semantics.

Config lives in ``~/.ssr/remote.json`` (endpoint + token + node name + tags) and
is created interactively on first run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Settings

try:  # POSIX pseudo-terminal support
    import fcntl
    import pty
    import struct
    import termios

    _HAS_PTY = True
except ImportError:  # pragma: no cover - Windows
    _HAS_PTY = False

_MAX_READ = 2_000_000  # cap a single fs.read at ~2 MB


# --------------------------------------------------------------------- config
@dataclass
class RemoteConfig:
    endpoint: str            # e.g. https://dispatch.example.com:8787
    token: str
    node_name: str
    tags: list[str] = field(default_factory=list)

    def ws_url(self) -> str:
        """Derive the node WebSocket URL from the configured HTTP endpoint."""
        ep = self.endpoint.rstrip("/")
        if ep.startswith("https://"):
            ep = "wss://" + ep[len("https://"):]
        elif ep.startswith("http://"):
            ep = "ws://" + ep[len("http://"):]
        elif not (ep.startswith("ws://") or ep.startswith("wss://")):
            ep = "ws://" + ep
        return ep + "/ws/node"


def config_path(settings: Settings) -> Path:
    return settings.home / "remote.json"


def load_config(settings: Settings) -> RemoteConfig | None:
    p = config_path(settings)
    if not p.exists():
        return None
    try:
        return RemoteConfig(**json.loads(p.read_text("utf-8")))
    except Exception:
        return None


def save_config(settings: Settings, cfg: RemoteConfig) -> Path:
    p = config_path(settings)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), "utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def configure_interactive(settings: Settings) -> RemoteConfig:
    """Prompt for the dispatch endpoint, token, node name and tags."""
    existing = load_config(settings)

    def ask(label: str, current: str = "", secret: bool = False) -> str:
        suffix = f" [{'•' * 6 if secret and current else current}]" if current else ""
        val = input(f"{label}{suffix}: ").strip()
        return val or current

    print("\n配置远程控制 (SSR remote control)\n" + "-" * 36)
    endpoint = ask("Dispatch 服务器端点 endpoint (http[s]://host:port)",
                   existing.endpoint if existing else "")
    token = ask("API token", existing.token if existing else "", secret=True)
    default_name = (existing.node_name if existing else "") or platform.node() or "ssr-node"
    node_name = ask("节点名 node name", default_name)
    tags_raw = ask("标签 tags (comma-separated)",
                   ", ".join(existing.tags) if existing else "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    cfg = RemoteConfig(endpoint=endpoint, token=token, node_name=node_name, tags=tags)
    path = save_config(settings, cfg)
    print(f"\n已保存到 {path}")
    return cfg


# ---------------------------------------------------------------- PTY session
class PtySession:
    """One interactive shell bound to a PTY, streaming output to a callback.

    ``on_output(data: bytes)`` and ``on_exit(code: int)`` are invoked from a
    reader thread; the caller is responsible for marshalling them to its loop.
    """

    def __init__(self, cwd: str, on_output, on_exit):
        self.on_output = on_output
        self.on_exit = on_exit
        self.cwd = _expand(cwd)
        self._master_fd: int | None = None
        self._proc: subprocess.Popen | None = None
        self._alive = True

    def start(self) -> None:
        import threading

        shell = os.environ.get("SHELL") or ("cmd.exe" if os.name == "nt" else "/bin/bash")
        cwd = self.cwd if os.path.isdir(self.cwd) else str(Path.home())
        if _HAS_PTY:
            master, slave = pty.openpty()
            self._master_fd = master
            self._proc = subprocess.Popen(
                [shell, "-i"] if shell.endswith("bash") or shell.endswith("sh") else [shell],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                start_new_session=True,
                env={**os.environ, "TERM": os.environ.get("TERM", "xterm-256color")},
            )
            os.close(slave)
            threading.Thread(target=self._read_pty, daemon=True).start()
        else:  # pragma: no cover - non-POSIX fallback
            self._proc = subprocess.Popen(
                [shell],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                bufsize=0,
            )
            threading.Thread(target=self._read_pipe, daemon=True).start()

    def _read_pty(self) -> None:
        assert self._master_fd is not None
        try:
            while self._alive:
                try:
                    data = os.read(self._master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.on_output(data)
        finally:
            code = self._proc.wait() if self._proc else 0
            self._alive = False
            self.on_exit(code)

    def _read_pipe(self) -> None:  # pragma: no cover - non-POSIX fallback
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while self._alive:
                data = self._proc.stdout.read(4096)
                if not data:
                    break
                self.on_output(data)
        finally:
            code = self._proc.wait() if self._proc else 0
            self._alive = False
            self.on_exit(code)

    def write(self, data: str) -> None:
        raw = data.encode("utf-8", errors="replace")
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, raw)
            except OSError:
                pass
        elif self._proc and self._proc.stdin:  # pragma: no cover
            try:
                self._proc.stdin.write(raw)
                self._proc.stdin.flush()
            except OSError:
                pass

    def resize(self, cols: int, rows: int) -> None:
        if self._master_fd is not None and _HAS_PTY:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    def close(self) -> None:
        self._alive = False
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                if _HAS_PTY:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:  # pragma: no cover
                    proc.terminate()
            except (OSError, ProcessLookupError):
                pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None


# ----------------------------------------------------------------- node loop
class RemoteNode:
    """Maintains the dispatch connection and serves inbound RPCs."""

    def __init__(self, settings: Settings, cfg: RemoteConfig):
        self.settings = settings
        self.cfg = cfg
        self.ws = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, PtySession] = {}
        self._agent = None  # lazily constructed SSRAgent for agent.run

    # ------------------------------------------------------------- connection
    async def serve_forever(self) -> None:
        """Connect (with reconnect/backoff) and serve until interrupted."""
        import websockets

        self.loop = asyncio.get_running_loop()
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.cfg.ws_url(), max_size=16 * 1024 * 1024) as ws:
                    self.ws = ws
                    await self._handshake(ws)
                    backoff = 1
                    await asyncio.gather(self._recv_loop(ws), self._heartbeat(ws))
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:
                print(f"[rc] disconnected: {e}; reconnecting in {backoff}s", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _handshake(self, ws) -> None:
        await ws.send(json.dumps({
            "type": "hello",
            "token": self.cfg.token,
            "name": self.cfg.node_name,
            "tags": self.cfg.tags,
            "platform": platform.platform(),
            "cwd": str(self.settings.project_dir),
            "version": _ssr_version(),
        }))
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("type") != "welcome":
            raise RuntimeError(msg.get("message", "handshake rejected"))
        print(f"[rc] connected as node '{self.cfg.node_name}' "
              f"(id={msg.get('node_id')}) tags={self.cfg.tags or '∅'}", file=sys.stderr)

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"type": "heartbeat", "cwd": str(self.settings.project_dir)}))

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if msg.get("type") == "rpc":
                asyncio.ensure_future(self._handle_rpc(ws, msg))

    # ------------------------------------------------------------------- RPCs
    async def _handle_rpc(self, ws, msg: dict) -> None:
        rid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        try:
            result = await self._dispatch(method, params)
            ok, payload = True, result
        except Exception as e:
            ok, payload = False, f"{type(e).__name__}: {e}"
        if rid is None:
            return  # notification — no reply expected
        reply = {"type": "rpc_result", "id": rid, "ok": ok}
        if ok:
            reply["result"] = payload
        else:
            reply["error"] = payload
        await ws.send(json.dumps(reply, ensure_ascii=False))

    async def _dispatch(self, method: str, params: dict):
        if method == "fs.list":
            return await asyncio.to_thread(_fs_list, params.get("path", "~"))
        if method == "fs.read":
            return await asyncio.to_thread(_fs_read, params.get("path", ""))
        if method == "fs.write":
            return await asyncio.to_thread(
                _fs_write, params.get("path", ""), params.get("data_b64", "")
            )
        if method == "run_command":
            return await asyncio.to_thread(
                _run_command, params.get("command", ""), params.get("cwd", ""),
                int(params.get("timeout", 120)),
            )
        if method == "agent.run":
            return await asyncio.to_thread(
                self._run_agent, params.get("prompt", ""), params.get("cwd", "")
            )
        if method == "terminal.open":
            return self._terminal_open(params.get("cwd", "~"))
        if method == "terminal.input":
            self._terminal_input(params.get("session_id", ""), params.get("data", ""))
            return {"ok": True}
        if method == "terminal.resize":
            self._terminal_resize(
                params.get("session_id", ""), int(params.get("cols", 80)), int(params.get("rows", 24))
            )
            return {"ok": True}
        if method == "terminal.close":
            self._terminal_close(params.get("session_id", ""))
            return {"ok": True}
        if method == "ping":
            return {"pong": True}
        raise ValueError(f"unknown method: {method}")

    # ----------------------------------------------------------- terminal RPCs
    def _terminal_open(self, cwd: str) -> dict:
        import uuid

        session_id = uuid.uuid4().hex
        loop = self.loop

        def on_output(data: bytes) -> None:
            if loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._send_event(session_id, "term.output",
                                     base64.b64encode(data).decode("ascii")),
                    loop,
                )

        def on_exit(code: int) -> None:
            if loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._send_event(session_id, "term.exit", None, code=code), loop
                )
            self._sessions.pop(session_id, None)

        session = PtySession(cwd, on_output, on_exit)
        session.start()
        self._sessions[session_id] = session
        return {"session_id": session_id}

    def _terminal_input(self, session_id: str, data: str) -> None:
        sess = self._sessions.get(session_id)
        if sess:
            sess.write(data)

    def _terminal_resize(self, session_id: str, cols: int, rows: int) -> None:
        sess = self._sessions.get(session_id)
        if sess:
            sess.resize(cols, rows)

    def _terminal_close(self, session_id: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if sess:
            sess.close()

    async def _send_event(self, channel: str, event: str, data, **extra) -> None:
        if self.ws is None:
            return
        frame = {"type": "event", "channel": channel, "event": event, "data": data, **extra}
        try:
            await self.ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            pass

    # --------------------------------------------------------------- agent RPC
    def _run_agent(self, prompt: str, cwd: str) -> dict:
        from ..agent.core import SSRAgent

        if not prompt:
            return {"text": "ERROR: empty prompt"}
        # A fresh agent per dispatch, rooted at the requested directory.
        agent_settings = self.settings
        if cwd:
            agent_settings = Settings(
                home=self.settings.home,
                project_dir=_expand_path(cwd),
                gemini_api_key=self.settings.gemini_api_key,
                tavily_api_key=self.settings.tavily_api_key,
                default_model=self.settings.default_model,
            )
        agent = SSRAgent(agent_settings)
        try:
            return {"text": agent.run(prompt)}
        finally:
            agent.close()


# ------------------------------------------------------------ fs RPC helpers
def _expand(path: str) -> str:
    return str(_expand_path(path))


def _expand_path(path: str) -> Path:
    if not path:
        return Path.home()
    return Path(path).expanduser()


def _fs_list(path: str) -> dict:
    base = _expand_path(path)
    if not base.exists():
        raise FileNotFoundError(f"no such path: {base}")
    if base.is_file():
        base = base.parent
    entries = []
    for e in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            is_dir = e.is_dir()
            size = None if is_dir else e.stat().st_size
        except OSError:
            is_dir, size = False, None
        entries.append({"name": e.name, "path": str(e), "is_dir": is_dir, "size": size})
    return {"path": str(base), "entries": entries}


def _fs_read(path: str) -> dict:
    p = _expand_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    data = p.read_bytes()[:_MAX_READ]
    return {"name": p.name, "path": str(p), "size": len(data),
            "data_b64": base64.b64encode(data).decode("ascii")}


def _fs_write(path: str, data_b64: str) -> dict:
    p = _expand_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(data_b64) if data_b64 else b""
    p.write_bytes(data)
    return {"path": str(p), "bytes": len(data)}


def _run_command(command: str, cwd: str, timeout: int) -> dict:
    workdir = _expand(cwd) if cwd else os.getcwd()
    if not os.path.isdir(workdir):
        workdir = os.getcwd()
    try:
        proc = subprocess.run(
            command, shell=True, cwd=workdir, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"exit": -1, "output": f"ERROR: command timed out after {timeout}s"}
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    return {"exit": proc.returncode, "output": out[:100_000]}


def _ssr_version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:
        return "0.0.0"


# ----------------------------------------------------------------- entrypoint
def run_remote(settings: Settings, reconfigure: bool = False) -> int:
    """``ssr rc`` — configure on first run, then connect and serve."""
    if _import_websockets() is None:
        print(
            "Remote control needs the 'websockets' package.\n"
            f"  Install it:  {sys.executable} -m pip install websockets",
            file=sys.stderr,
        )
        return 1

    cfg = load_config(settings)
    if cfg is None or reconfigure or not cfg.endpoint or not cfg.token:
        cfg = configure_interactive(settings)
    if not cfg.endpoint or not cfg.token:
        print("[rc] endpoint and token are required.", file=sys.stderr)
        return 1

    node = RemoteNode(settings, cfg)
    print(f"[rc] connecting to {cfg.endpoint} …", file=sys.stderr)
    try:
        asyncio.run(node.serve_forever())
    except KeyboardInterrupt:
        print("\n[rc] stopped.", file=sys.stderr)
    return 0


def set_tags(settings: Settings, tags: list[str]) -> int:
    """``ssr rc tags a,b,c`` — update this node's tags in the local config."""
    cfg = load_config(settings)
    if cfg is None:
        print("[rc] not configured yet. Run `ssr rc` first.", file=sys.stderr)
        return 1
    cfg.tags = [t.strip() for t in tags if t.strip()]
    save_config(settings, cfg)
    print(f"[rc] tags set to {cfg.tags}. They apply on next connect.")
    return 0


def show_status(settings: Settings) -> int:
    cfg = load_config(settings)
    if cfg is None:
        print("[rc] not configured. Run `ssr rc` to set up remote control.")
        return 0
    print(f"endpoint:  {cfg.endpoint}")
    print(f"node name: {cfg.node_name}")
    print(f"tags:      {', '.join(cfg.tags) or '∅'}")
    print(f"ws url:    {cfg.ws_url()}")
    print(f"token:     {'•' * 8} (stored in {config_path(settings)})")
    return 0


def _import_websockets():
    try:
        import websockets  # noqa: F401

        return websockets
    except ImportError:
        return None
