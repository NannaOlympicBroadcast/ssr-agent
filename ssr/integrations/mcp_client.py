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
import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_LOGGER = logging.getLogger("ssr.mcp")

PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT = 30.0
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

    # ------------------------------------------------------------- lifecycle
    def start(self) -> list[MCPTool]:
        """Spawn the process, handshake and list tools. Returns the tools."""
        if self._started:
            return self._tools
        full_env = {**os.environ, **{str(k): str(v) for k, v in self.env.items()}}
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
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

        self._reader = threading.Thread(target=self._read_loop, name=f"mcp-{self.name}", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name=f"mcp-{self.name}-err", daemon=True
        )
        self._stderr_reader.start()

        # Handshake.
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

    def call(self, tool: str, arguments: Optional[dict] = None) -> str:
        """Invoke ``tool`` and return its result content flattened to text."""
        if not self._started:
            raise MCPError(f"MCP server '{self.name}' is not running")
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        return _flatten_content(result)

    def stop(self) -> None:
        """Terminate the subprocess (graceful, then forced)."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
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


class MCPManager:
    """Spawns and routes calls across all MCP servers declared in a config."""

    def __init__(self, servers: Optional[list[MCPServer]] = None):
        self._servers: dict[str, MCPServer] = {s.name: s for s in (servers or [])}
        self._tools: dict[str, MCPTool] = {}  # qualified_name -> tool
        self._started = False
        atexit.register(self.shutdown)

    @classmethod
    def from_config(cls, config_path: Path, timeout: float = _DEFAULT_TIMEOUT) -> "MCPManager":
        servers: list[MCPServer] = []
        for name, cfg in _read_server_configs(config_path).items():
            if cfg.get("disabled"):
                continue
            command = cfg.get("command")
            if not command:
                _LOGGER.warning("mcp server '%s' has no 'command'; skipping", name)
                continue
            servers.append(
                MCPServer(
                    name=name,
                    command=command,
                    args=cfg.get("args"),
                    env=cfg.get("env"),
                    cwd=cfg.get("cwd"),
                    timeout=timeout,
                )
            )
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
        return self.tools

    @property
    def tools(self) -> list[MCPTool]:
        return list(self._tools.values())

    def has_tools(self) -> bool:
        return bool(self._tools)

    def call_tool(self, qualified_name: str, arguments: Optional[dict] = None) -> str:
        """Route a ``mcp__<server>__<tool>`` call to the owning server."""
        tool = self._tools.get(qualified_name)
        if tool is None:
            server_name, tool_name = split_qualified_name(qualified_name)
        else:
            server_name, tool_name = tool.server, tool.name
        server = self._servers.get(server_name)
        if server is None:
            raise MCPError(f"unknown MCP server '{server_name}'")
        return server.call(tool_name, arguments)

    def shutdown(self) -> None:
        for server in self._servers.values():
            try:
                server.stop()
            except Exception:  # pragma: no cover - best-effort teardown
                pass


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


def _flatten_content(result: Any) -> str:
    """Turn an MCP ``tools/call`` result into a plain text string."""
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
        elif btype in ("image", "audio"):
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
