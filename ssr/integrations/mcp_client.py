"""Minimal Model Context Protocol (MCP) client over stdio.

This implements just enough of the MCP spec for the agent to use external tool
servers declared in ``~/.ssr/mcp.json``:

* spawn each server as a subprocess and speak JSON-RPC 2.0 over its stdio,
  using the newline-delimited framing the MCP stdio transport mandates;
* perform the ``initialize`` / ``notifications/initialized`` handshake;
* enumerate tools via ``tools/list``;
* invoke them via ``tools/call`` and flatten the result content to text.

It is deliberately synchronous (a reader thread per server matches responses to
request ids), since the agent's tool loop is synchronous. Server start-up and
calls are defensive: a misbehaving server is skipped/raised rather than taking
the whole agent down, and all processes are torn down on exit.

Config format (``mcpServers`` or ``servers`` key)::

    {
      "mcpServers": {
        "everything": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-everything"],
          "env": {"FOO": "bar"},
          "cwd": "/optional/working/dir",
          "disabled": false
        }
      }
    }
"""

from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger("ssr.mcp")

PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT = 120.0  # generous: some MCP servers (e.g. miot) do heavy init
_QUALIFIED_SEP = "__"


class MCPError(RuntimeError):
    """Raised when an MCP server cannot be reached or a call fails."""


@dataclass
class MCPTool:
    """A single tool exposed by an MCP server."""

    server: str
    name: str
    description: str
    input_schema: dict

    @property
    def qualified_name(self) -> str:
        """Schema-safe name exposed to the model, e.g. ``mcp__everything__echo``."""
        return f"mcp{_QUALIFIED_SEP}{self.server}{_QUALIFIED_SEP}{self.name}"


def is_mcp_tool_name(name: str) -> bool:
    return name.startswith(f"mcp{_QUALIFIED_SEP}")


def split_qualified_name(name: str) -> tuple[str, str]:
    """``mcp__<server>__<tool>`` → ``(server, tool)``. Tool may contain ``__``."""
    if not is_mcp_tool_name(name):
        raise MCPError(f"not an MCP tool name: {name!r}")
    rest = name[len(f"mcp{_QUALIFIED_SEP}"):]
    server, sep, tool = rest.partition(_QUALIFIED_SEP)
    if not sep:
        raise MCPError(f"malformed MCP tool name: {name!r}")
    return server, tool


class MCPServer:
    """Manages one MCP server subprocess and its JSON-RPC stdio session."""

    def __init__(
        self,
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.cwd = cwd
        self.timeout = timeout

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}     # id -> {"event", "result", "error"}
        self._tools: list[MCPTool] = []
        self._started = False
        self._closed = False
        self._job = None  # Windows Job handle: kills the child if we are killed

    # ------------------------------------------------------------- lifecycle
    def start(self) -> list[MCPTool]:
        """Spawn the process, handshake and list tools. Returns the tools."""
        if self._started:
            return self._tools
        full_env = {**os.environ, **{str(k): str(v) for k, v in self.env.items()}}
        popen_args = _resolve_spawn_args(self.command, self.args)
        try:
            self._proc = subprocess.Popen(
                popen_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=full_env,
                # Force binary mode for stdout/stderr to avoid encoding errors
                bufsize=1,
                # Ensure we handle text encoding explicitly if needed,
                # but reading bytes is safer for JSON-RPC lines
            )
        except (OSError, ValueError) as e:
            raise MCPError(f"failed to spawn MCP server '{self.name}': {e}") from e

        # Tie the child's lifetime to ours on Windows so it can't outlive us as
        # an orphan if we are hard-killed (e.g. pm2 restarting the gateway).
        self._job = _assign_kill_on_close_job(self._proc.pid)

        self._reader = threading.Thread(target=self._read_loop, name=f"mcp-{self.name}", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name=f"mcp-{self.name}-err", daemon=True
        )
        self._stderr_reader.start()

        # Handshake + tool enumeration. If any step fails or times out (e.g. a
        # server that blocks on interactive auth), kill the spawned subprocess
        # before propagating — otherwise it leaks as an orphan that accumulates
        # on every agent start / gateway restart.
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ssr-agent", "version": "0.1.0"},
                },
            )
            self._notify("notifications/initialized")

            # Enumerate tools.
            result = self._request("tools/list", {})
        except Exception:
            self.stop()
            raise
        self._tools = [
            MCPTool(
                server=self.name,
                name=t.get("name", ""),
                description=t.get("description", "") or "",
                input_schema=t.get("inputSchema") or t.get("input_schema") or {"type": "object"},
            )
            for t in (result.get("tools") or [])
            if t.get("name")
        ]
        self._started = True
        return self._tools

    @property
    def tools(self) -> list[MCPTool]:
        return list(self._tools)

    def call(self, tool: str, arguments: Optional[dict] = None, on_image=None) -> str:
        """Invoke ``tool`` and return its result content flattened to text.

        ``on_image(data: bytes, mime_type: str)``, if given, receives the decoded
        bytes of any ``image`` content block instead of it being discarded — see
        :func:`_flatten_content`.
        """
        if not self._started:
            raise MCPError(f"MCP server '{self.name}' is not running")
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        return _flatten_content(result, on_image=on_image)

    def _close_job(self) -> None:
        """Close the Windows Job handle (kills any process still inside it)."""
        job = self._job
        self._job = None
        if job is not None:
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(job)  # type: ignore[attr-defined]
            except Exception:
                pass

    def stop(self) -> None:
        """Terminate the subprocess (graceful, then forced)."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            self._close_job()
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        # Closing the job handle kills anything still alive in it (a last resort).
        self._close_job()
        # Fail any in-flight requests so callers don't hang.
        with self._lock:
            for slot in self._pending.values():
                slot.setdefault("error", {"message": "MCP server stopped"})
                slot["event"].set()
            self._pending.clear()

    # --------------------------------------------------------------- transport
    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            # Handle binary lines
            try:
                line_str = line.decode('utf-8', errors='replace').strip()
            except Exception:
                _LOGGER.debug("mcp[%s] decode error, skipping line", self.name)
                continue

            if not line_str:
                continue
            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                _LOGGER.debug("mcp[%s] non-JSON line: %s", self.name, line_str[:200])
                continue
            mid = msg.get("id")
            if mid is None:
                continue  # a notification/request from the server — ignored
            with self._lock:
                slot = self._pending.get(mid)
            if slot is None:
                continue
            if "error" in msg:
                slot["error"] = msg["error"]
            else:
                slot["result"] = msg.get("result", {})
            slot["event"].set()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            try:
                _LOGGER.debug("mcp[%s] stderr: %s", self.name, line.decode('utf-8', errors='replace').rstrip())
            except Exception:
                pass

    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise MCPError(f"MCP server '{self.name}' is not available")
        # Ensure encoding to bytes for stdin
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode('utf-8')
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MCPError(f"failed to write to MCP server '{self.name}': {e}") from e

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict) -> dict:
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            event = threading.Event()
            self._pending[mid] = {"event": event}
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        if not event.wait(timeout=self.timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise MCPError(f"MCP server '{self.name}' timed out on '{method}'")
        with self._lock:
            slot = self._pending.pop(mid, {})
        if "error" in slot:
            err = slot["error"]
            raise MCPError(f"MCP '{self.name}' {method} error: {err.get('message', err)}")
        return slot.get("result", {})

class MCPSSEServer:
    """Manages one MCP server connection over SSE + HTTP transport."""

    def __init__(
        self,
        name: str,
        url: str,
        headers: Optional[dict[str, str]] = None,
        query_params: Optional[dict[str, str]] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.name = name
        self.url = url
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.timeout = timeout

        self._client: Optional[Any] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}     # id -> {"event", "result", "error"}
        self._tools: list[MCPTool] = []
        self._started = False
        self._closed = False
        self._post_url: Optional[str] = None
        self._endpoint_ready = threading.Event()

    def start(self) -> list[MCPTool]:
        """Establish SSE connection, handshake and list tools. Returns the tools."""
        if self._started:
            return self._tools

        import httpx
        self._client = httpx.Client(headers=self.headers, timeout=self.timeout)

        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-sse-{self.name}", daemon=True
        )
        self._reader.start()

        # Wait for endpoint advertisement from the server.
        if not self._endpoint_ready.wait(timeout=self.timeout):
            self.stop()
            raise MCPError(f"MCP SSE server '{self.name}' timed out waiting for endpoint advertisement")

        # Handshake
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ssr-agent", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized")

        # Enumerate tools
        result = self._request("tools/list", {})
        self._tools = [
            MCPTool(
                server=self.name,
                name=t.get("name", ""),
                description=t.get("description", "") or "",
                input_schema=t.get("inputSchema") or t.get("input_schema") or {"type": "object"},
            )
            for t in (result.get("tools") or [])
            if t.get("name")
        ]
        self._started = True
        return self._tools

    @property
    def tools(self) -> list[MCPTool]:
        return list(self._tools)

    def call(self, tool: str, arguments: Optional[dict] = None, on_image=None) -> str:
        """Invoke ``tool`` and return its result content flattened to text.

        ``on_image(data: bytes, mime_type: str)``, if given, receives the decoded
        bytes of any ``image`` content block instead of it being discarded — see
        :func:`_flatten_content`.
        """
        if not self._started:
            raise MCPError(f"MCP server '{self.name}' is not running")
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        return _flatten_content(result, on_image=on_image)

    def stop(self) -> None:
        """Close HTTP clients and connection."""
        if self._closed:
            return
        self._closed = True
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

        # Fail any in-flight requests so callers don't hang.
        with self._lock:
            for slot in self._pending.values():
                slot.setdefault("error", {"message": "MCP server stopped"})
                slot["event"].set()
            self._pending.clear()

    def _read_loop(self) -> None:
        import urllib.parse
        try:
            # NB: httpx replaces (not merges) the URL's own query string whenever a
            # ``params`` value is passed — even an empty dict. Pass ``None`` when we
            # have no extra params so a key embedded in the URL (e.g. the Hosted
            # Tools ``/sse/integrations?key=<mcp_key>``) survives instead of being
            # stripped, which would make the SSE GET hit the proxy unauthenticated (401).
            with self._client.stream("GET", self.url, params=self.query_params or None, timeout=None) as response:
                if response.status_code != 200:
                    _LOGGER.error("mcp-sse[%s] connection failed with status code %s", self.name, response.status_code)
                    self._endpoint_ready.set()
                    return

                current_event = None
                current_data_lines = []

                for line in response.iter_lines():
                    if self._closed:
                        break
                    line = line.strip()
                    if not line:
                        if current_event or current_data_lines:
                            data = "\n".join(current_data_lines)
                            self._handle_sse_event(current_event, data)
                            current_event = None
                            current_data_lines = []
                        continue

                    if line.startswith(":"):
                        continue

                    if ":" in line:
                        field, value = line.split(":", 1)
                        field = field.strip()
                        value = value.strip()
                        if field == "event":
                            current_event = value
                        elif field == "data":
                            current_data_lines.append(value)

                if current_event or current_data_lines:
                    data = "\n".join(current_data_lines)
                    self._handle_sse_event(current_event, data)

        except Exception as e:
            if not self._closed:
                _LOGGER.error("mcp-sse[%s] reader loop error: %s", self.name, e)
        finally:
            self._endpoint_ready.set()
            # Fail any in-flight requests so callers don't hang.
            with self._lock:
                for slot in self._pending.values():
                    slot.setdefault("error", {"message": "MCP server connection lost"})
                    slot["event"].set()
                self._pending.clear()

    def _handle_sse_event(self, event: Optional[str], data: str) -> None:
        import urllib.parse
        if event == "endpoint":
            self._post_url = urllib.parse.urljoin(self.url, data)
            self._endpoint_ready.set()
        elif event == "message" or event is None:
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                _LOGGER.debug("mcp-sse[%s] non-JSON line: %s", self.name, data[:200])
                return
            mid = msg.get("id")
            if mid is None:
                return  # A notification/request from the server, ignored
            with self._lock:
                slot = self._pending.get(mid)
            if slot is None:
                return
            if "error" in msg:
                slot["error"] = msg["error"]
            else:
                slot["result"] = msg.get("result", {})
            slot["event"].set()

    def _send(self, payload: dict) -> None:
        if not self._post_url:
            raise MCPError(f"MCP server '{self.name}' has no post URL")
        try:
            r = self._client.post(self._post_url, json=payload)
            if r.status_code not in (200, 202):
                raise MCPError(f"POST failed with status {r.status_code}: {r.text}")
        except Exception as e:
            raise MCPError(f"failed to write to MCP server '{self.name}': {e}") from e

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict) -> dict:
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            event = threading.Event()
            self._pending[mid] = {"event": event}
        try:
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        except Exception:
            with self._lock:
                self._pending.pop(mid, None)
            raise
        if not event.wait(timeout=self.timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise MCPError(f"MCP server '{self.name}' timed out on '{method}'")
        with self._lock:
            slot = self._pending.pop(mid, {})
        if "error" in slot:
            err = slot["error"]
            raise MCPError(f"MCP '{self.name}' {method} error: {err.get('message', err)}")
        return slot.get("result", {})


class MCPManager:
    """Spawns and routes calls across all MCP servers declared in a config."""

    def __init__(self, servers: Optional[list[MCPServer | MCPSSEServer]] = None):
        self._servers: dict[str, MCPServer | MCPSSEServer] = {s.name: s for s in (servers or [])}
        self._tools: dict[str, MCPTool] = {}  # qualified_name -> tool
        self._started = False
        atexit.register(self.shutdown)

    @classmethod
    def from_config(cls, config_path: Path, timeout: float = _DEFAULT_TIMEOUT) -> "MCPManager":
        return cls.from_server_configs(_read_server_configs(config_path), timeout=timeout)

    @classmethod
    def from_server_configs(
        cls, server_configs: dict[str, dict], timeout: float = _DEFAULT_TIMEOUT
    ) -> "MCPManager":
        """Build a manager from an already-merged ``{name: cfg}`` mapping.

        This is the shared path for both ``mcp.json`` and bundled/user *plugins*
        (which contribute additional ``mcpServers``).
        """
        servers: list[MCPServer | MCPSSEServer] = []
        for name, cfg in (server_configs or {}).items():
            if cfg.get("disabled"):
                continue
            if "url" in cfg:
                servers.append(
                    MCPSSEServer(
                        name=name,
                        url=cfg["url"],
                        headers=cfg.get("headers"),
                        query_params=cfg.get("query_params"),
                        timeout=timeout,
                    )
                )
            elif "command" in cfg:
                servers.append(
                    MCPServer(
                        name=name,
                        command=cfg["command"],
                        args=cfg.get("args"),
                        env=cfg.get("env"),
                        cwd=cfg.get("cwd"),
                        timeout=timeout,
                    )
                )
            else:
                _LOGGER.warning("mcp server '%s' has neither 'command' nor 'url'; skipping", name)
        return cls(servers)

    @classmethod
    def from_settings(cls, settings: Any, timeout: float = _DEFAULT_TIMEOUT) -> "MCPManager":
        servers: list[MCPServer | MCPSSEServer] = []

        # Lazily resolve ``${namespace.key}`` credential placeholders from the
        # shared ``~/.ssr/<namespace>.json`` files (used by e.g. the miot plugin).
        try:
            from ..plugins import _resolve_placeholders, load_credentials

            _creds_cache: dict[str, dict] = {}

            def _creds_for(ns: str) -> dict:
                if ns not in _creds_cache:
                    _creds_cache[ns] = load_credentials(settings, ns)
                return _creds_cache[ns]

            def _resolve_creds(cfg: dict) -> dict:
                return _resolve_placeholders(cfg, _creds_for)
        except Exception:  # pragma: no cover - plugins module always present
            def _resolve_creds(cfg: dict) -> dict:
                return cfg

        # 1. Load from main mcp.json
        if hasattr(settings, "mcp_config"):
            for name, cfg in _read_server_configs(settings.mcp_config).items():
                if cfg.get("disabled"):
                    continue
                if "url" in cfg:
                    servers.append(
                        MCPSSEServer(
                            name=name,
                            url=cfg["url"],
                            headers=cfg.get("headers"),
                            query_params=cfg.get("query_params"),
                            timeout=timeout,
                        )
                    )
                elif "command" in cfg:
                    servers.append(
                        MCPServer(
                            name=name,
                            command=cfg["command"],
                            args=cfg.get("args"),
                            env=cfg.get("env"),
                            cwd=cfg.get("cwd"),
                            timeout=timeout,
                        )
                    )
                else:
                    _LOGGER.warning("mcp server '%s' has neither 'command' nor 'url'; skipping", name)
                
        # 2. Load from plugins
        plugin_configs = find_plugin_mcp_configs(settings)
        for plugin_dir, servers_dict in plugin_configs:
            dirname = str(plugin_dir.resolve()).replace("\\", "/")
            for name, cfg in servers_dict.items():
                cfg = _resolve_creds(cfg)
                if cfg.get("disabled"):
                    continue
                if "url" in cfg:
                    servers.append(
                        MCPSSEServer(
                            name=name,
                            url=cfg["url"],
                            headers=cfg.get("headers"),
                            query_params=cfg.get("query_params"),
                            timeout=timeout,
                        )
                    )
                elif "command" in cfg:
                    # Resolve __dirname in command, args, env, cwd
                    resolved_command = resolve_dirnames(cfg["command"], dirname)
                    resolved_args = resolve_dirnames(cfg.get("args"), dirname)
                    resolved_env = resolve_dirnames(cfg.get("env"), dirname)
                    resolved_cwd = resolve_dirnames(cfg.get("cwd"), dirname) or dirname
                    
                    servers.append(
                        MCPServer(
                            name=name,
                            command=resolved_command,
                            args=resolved_args,
                            env=resolved_env,
                            cwd=resolved_cwd,
                            timeout=timeout,
                        )
                    )
                else:
                    _LOGGER.warning("plugin mcp server '%s' has neither 'command' nor 'url'; skipping", name)

        return cls(servers)

    def start_all(self) -> list[MCPTool]:
        """Start every configured server (skipping ones that fail) and collect tools."""
        self._started = True
        for name, server in self._servers.items():
            try:
                for tool in server.start():
                    self._tools[tool.qualified_name] = tool
            except Exception as e:  # one bad server must not break the agent
                _LOGGER.warning("mcp server '%s' failed to start: %s", name, e)
                # Make sure a server that failed mid-startup doesn't leave its
                # subprocess running (orphaned).
                try:
                    server.stop()
                except Exception:
                    pass
        return self.tools

    @property
    def tools(self) -> list[MCPTool]:
        return list(self._tools.values())

    def has_tools(self) -> bool:
        return bool(self._tools)

    def call_tool(self, qualified_name: str, arguments: Optional[dict] = None, agent=None) -> str:
        """Route a ``mcp__<server>__<tool>`` call to the owning server.

        ``agent``, if given and it implements ``queue_tool_image`` (see
        :class:`ssr.agent.core.SSRAgent`), receives any image content block the
        tool's result carries — e.g. a screenshot tool's actual picture — so it
        gets attached to the model's next turn instead of collapsing to a
        "[image ...]" placeholder caption the model can't act on.
        """
        tool = self._tools.get(qualified_name)
        if tool is None:
            server_name, tool_name = split_qualified_name(qualified_name)
        else:
            server_name, tool_name = tool.server, tool.name
        server = self._servers.get(server_name)
        if server is None:
            raise MCPError(f"unknown MCP server '{server_name}'")
        on_image = None
        if agent is not None and hasattr(agent, "queue_tool_image"):
            on_image = lambda data, mime, _name=qualified_name: agent.queue_tool_image(
                data, mime_type=mime, label=f"mcp:{_name}")
        return server.call(tool_name, arguments, on_image=on_image)

    def shutdown(self) -> None:
        for server in self._servers.values():
            try:
                server.stop()
            except Exception:  # pragma: no cover - best-effort teardown
                pass


def _assign_kill_on_close_job(pid: int):
    """Put *pid* in a Windows Job that kills it when the job handle closes.

    The returned handle must be kept alive for as long as the child should live.
    When our process exits — even via an unclean kill (pm2 restart, crash) — the
    OS closes the handle, the job's ``KILL_ON_JOB_CLOSE`` limit fires, and the
    child is terminated instead of leaking as an orphan. No-op off Windows /
    on any error (best-effort), so behaviour is unchanged where unsupported.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        ULONG_PTR = ctypes.c_size_t

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ULONG_PTR),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo", _IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = _EXT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            return None

        kernel32.OpenProcess.restype = wintypes.HANDLE
        hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hproc:
            kernel32.CloseHandle(job)
            return None
        ok = kernel32.AssignProcessToJobObject(job, hproc)
        kernel32.CloseHandle(hproc)
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _resolve_spawn_args(command: str, args: list[str]) -> list[str]:
    """Build the argv for ``subprocess.Popen`` in a cross-platform way.

    On Windows, Node launchers such as ``npx``/``npm`` are ``.cmd`` shims that
    ``CreateProcess`` cannot find (it ignores ``PATHEXT``) or execute directly,
    which surfaces as ``[WinError 2] 系统找不到指定的文件``. Resolve the command
    through ``PATHEXT`` (``shutil.which``) and run ``.cmd``/``.bat`` shims via
    ``cmd /c``. On POSIX we resolve via ``which`` and otherwise pass through
    unchanged, so behaviour is identical to before.
    """
    args = list(args)
    resolved = shutil.which(command)
    if os.name == "nt":
        if resolved and resolved.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", resolved, *args]
        if resolved:
            return [resolved, *args]
        # Last resort: let the shell resolve it (handles shims not on PATHEXT).
        return ["cmd", "/c", command, *args]
    return [resolved or command, *args]


def _read_server_configs(config_path: Path) -> dict[str, dict]:
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _LOGGER.warning("could not read MCP config %s: %s", config_path, e)
        return {}
    servers = data.get("mcpServers", data.get("servers", {}))
    return servers if isinstance(servers, dict) else {}


def find_plugin_mcp_configs(settings: Any) -> list[tuple[Path, dict]]:
    """Return a list of (plugin_dir, servers_dict) for all discovered plugins that have MCP configs."""
    configs = []
    plugins_dirs = []
    if hasattr(settings, "plugins_dir"):
        plugins_dirs.append(settings.plugins_dir)
    if hasattr(settings, "project_plugins_dir"):
        plugins_dirs.append(settings.project_plugins_dir)
        
    for base in plugins_dirs:
        if not base.exists():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            
            # Check for plugin manifest
            manifest_path = None
            for cand in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
                path = entry / cand
                if path.exists():
                    manifest_path = path
                    break
            
            if manifest_path is None:
                continue
                
            servers_dict = {}
            # 1. Try reading inline mcpServers from plugin.json
            try:
                data = json.loads(manifest_path.read_text("utf-8"))
                servers_dict = data.get("mcpServers", data.get("servers", {}))
            except Exception:
                pass
                
            # 2. If inline not found or empty, try reading separate .mcp.json or mcp.json
            if not isinstance(servers_dict, dict) or not servers_dict:
                for cand in (".mcp.json", "mcp.json"):
                    mcp_path = entry / cand
                    if mcp_path.exists():
                        try:
                            data = json.loads(mcp_path.read_text("utf-8"))
                            servers_dict = data.get("mcpServers", data.get("servers", {}))
                        except Exception:
                            pass
                        break
                        
            if isinstance(servers_dict, dict) and servers_dict:
                configs.append((entry, servers_dict))
                
    return configs


def resolve_dirnames(val: Any, dirname: str) -> Any:
    if isinstance(val, str):
        val = val.replace("${__dirname}", dirname).replace("__dirname", dirname)
        val = val.replace("${CLAUDE_PLUGIN_ROOT}", dirname)
        return val
    elif isinstance(val, list):
        return [resolve_dirnames(item, dirname) for item in val]
    elif isinstance(val, dict):
        return {k: resolve_dirnames(v, dirname) for k, v in val.items()}
    return val


def _flatten_content(result: Any, on_image=None) -> str:
    """Turn an MCP ``tools/call`` result into a plain text string.

    The MCP spec allows ``content`` blocks of type ``image`` (base64 ``data`` +
    ``mimeType``), not just ``text`` — a screenshot tool is a normal, spec-legal
    MCP tool. When ``on_image`` is given, every image block's bytes are decoded
    and handed to it (``on_image(data: bytes, mime_type: str)``) instead of being
    silently thrown away into a placeholder caption — without this, EVERY MCP
    tool that returns a picture is invisible to the model, the exact same failure
    class as the fixed ``arm_get_camera`` bug (an agent that only ever reads
    "[image image/png]" text can't act on what's in the picture). Falls back to
    the old placeholder when no callback is given or a block fails to decode.
    """
    if not isinstance(result, dict):
        return str(result)
    parts: list[str] = []
    for block in result.get("content", []) or []:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "image":
            mime = block.get("mimeType") or "image/png"
            attached = False
            if on_image is not None:
                try:
                    data = base64.b64decode(block.get("data") or "")
                    if data:
                        on_image(data, mime)
                        attached = True
                except Exception:
                    attached = False
            parts.append("[image attached to this turn]" if attached else f"[image {mime}]")
        elif btype == "audio":
            parts.append(f"[{btype} {block.get('mimeType', '')}]")
        elif btype == "resource":
            res = block.get("resource", {})
            parts.append(res.get("text") or res.get("uri", "[resource]"))
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    text = "\n".join(p for p in parts if p)
    if result.get("isError"):
        return f"ERROR: {text or 'MCP tool reported an error'}"
    if not text and "structuredContent" in result:
        return json.dumps(result["structuredContent"], ensure_ascii=False)
    return text or "(no content)"
