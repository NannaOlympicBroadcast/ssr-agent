"""Real (no-mock) tests for the MCP client + its wiring into SSRAgent.

These spawn an actual MCP server subprocess (``mcp_echo_server.py``) and speak
the protocol over stdio end to end. They never call the Gemini API — the tool
routing is exercised directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ssr.config import Settings
from ssr.integrations.mcp_client import (
    MCPManager,
    is_mcp_tool_name,
    split_qualified_name,
    _flatten_content,
)

_SERVER = Path(__file__).parent / "mcp_echo_server.py"


def _write_config(home: Path, server_name: str = "echo") -> Path:
    cfg = {
        "mcpServers": {
            server_name: {"command": sys.executable, "args": [str(_SERVER)]},
            "disabled_one": {"command": sys.executable, "args": [str(_SERVER)], "disabled": True},
        }
    }
    path = home / "mcp.json"
    path.write_text(json.dumps(cfg), "utf-8")
    return path


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    s = Settings(home=home, project_dir=proj, default_model="gemini-3.1-flash-lite")
    s.ensure_dirs()
    return s


# --------------------------------------------------------------------- unit
def test_qualified_name_helpers():
    assert is_mcp_tool_name("mcp__srv__tool")
    assert not is_mcp_tool_name("read_file")
    assert split_qualified_name("mcp__srv__do_thing") == ("srv", "do_thing")
    # tool names may themselves contain the separator
    assert split_qualified_name("mcp__srv__a__b") == ("srv", "a__b")


def test_flatten_content():
    assert _flatten_content({"content": [{"type": "text", "text": "hi"}]}) == "hi"
    assert _flatten_content({"content": [{"type": "text", "text": "boom"}], "isError": True}).startswith("ERROR")
    assert "[image" in _flatten_content({"content": [{"type": "image", "mimeType": "image/png"}]})


def test_flatten_content_decodes_image_to_on_image_callback():
    # The MCP spec allows an "image" content block (base64 data + mimeType) —
    # without on_image the bytes are discarded into a caption the model can't
    # act on; with it, the real bytes reach the caller.
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    result = {"content": [
        {"type": "text", "text": "here is the screenshot"},
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()},
    ]}
    received = []
    text = _flatten_content(result, on_image=lambda data, mime: received.append((data, mime)))
    assert received == [(png, "image/png")]
    assert "here is the screenshot" in text
    assert "attached to this turn" in text


def test_flatten_content_falls_back_when_image_data_missing_or_bad():
    # Malformed/absent data must not raise — it degrades to the old placeholder.
    for block in ({"type": "image", "mimeType": "image/png"},
                  {"type": "image", "mimeType": "image/png", "data": "not-valid-base64!!"}):
        received = []
        text = _flatten_content({"content": [block]}, on_image=lambda d, m: received.append(d))
        assert received == []
        assert "[image" in text and "attached" not in text


# ----------------------------------------------------------- live subprocess
def test_manager_spawns_and_lists_tools(settings):
    path = _write_config(settings.home)
    mgr = MCPManager.from_config(path)
    try:
        tools = mgr.start_all()
        names = {t.qualified_name for t in tools}
        # disabled server is skipped; echo server exposes echo + add + snapshot
        assert names == {"mcp__echo__echo", "mcp__echo__add", "mcp__echo__snapshot"}
        assert mgr.has_tools()
    finally:
        mgr.shutdown()


def test_manager_calls_tools(settings):
    path = _write_config(settings.home)
    mgr = MCPManager.from_config(path)
    try:
        mgr.start_all()
        assert mgr.call_tool("mcp__echo__echo", {"text": "你好"}) == "你好"
        assert mgr.call_tool("mcp__echo__add", {"a": 2, "b": 3}) == "5"
    finally:
        mgr.shutdown()


def test_manager_call_tool_attaches_image_when_agent_supports_it(settings):
    # End-to-end over the REAL stdio subprocess: a spec-legal MCP tool returns an
    # "image" content block, and call_tool(..., agent=...) must decode it and
    # hand the actual bytes to agent.queue_tool_image — this is the fix for MCP
    # screenshot-style tools being invisible to the model (same bug class as the
    # arm_get_camera fix, just one layer down in the MCP client).
    import base64

    # Mirrors the constant in mcp_echo_server.py's "snapshot" tool.
    png_1px = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    class _StubAgent:
        def __init__(self):
            self.queued = []

        def queue_tool_image(self, data, mime_type="image/png", label=""):
            self.queued.append((data, mime_type, label))

    path = _write_config(settings.home)
    mgr = MCPManager.from_config(path)
    try:
        mgr.start_all()
        agent = _StubAgent()
        text = mgr.call_tool("mcp__echo__snapshot", {}, agent=agent)
        assert "attached to this turn" in text
        assert len(agent.queued) == 1
        data, mime, label = agent.queued[0]
        assert data == png_1px
        assert mime == "image/png"
        assert "mcp__echo__snapshot" in label

        # Without an agent (or one that doesn't support queue_tool_image), the
        # image must not raise and falls back to the old placeholder.
        assert "[image" in mgr.call_tool("mcp__echo__snapshot", {})
    finally:
        mgr.shutdown()


def test_shutdown_terminates_process(settings):
    path = _write_config(settings.home)
    mgr = MCPManager.from_config(path)
    mgr.start_all()
    proc = mgr._servers["echo"]._proc
    assert proc.poll() is None  # running
    mgr.shutdown()
    assert proc.poll() is not None  # terminated


def test_bad_server_is_isolated(settings):
    cfg = {"mcpServers": {"broken": {"command": "this-binary-does-not-exist-xyz", "args": []}}}
    (settings.home / "mcp.json").write_text(json.dumps(cfg), "utf-8")
    mgr = MCPManager.from_config(settings.home / "mcp.json")
    try:
        tools = mgr.start_all()  # must not raise
        assert tools == []
        assert not mgr.has_tools()
    finally:
        mgr.shutdown()


def test_hung_server_subprocess_is_killed_not_orphaned(settings):
    # A server that spawns but never speaks JSON-RPC → initialize times out.
    # The spawned subprocess must be terminated, not left running as an orphan
    # (which is what caused gateways to "spawn infinite processes").
    cfg = {"mcpServers": {"hang": {
        "command": sys.executable,
        "args": ["-c", "import time; time.sleep(60)"],
    }}}
    (settings.home / "mcp.json").write_text(json.dumps(cfg), "utf-8")
    mgr = MCPManager.from_config(settings.home / "mcp.json", timeout=2)
    try:
        tools = mgr.start_all()  # must not raise even though the server hangs
        assert tools == []
        proc = mgr._servers["hang"]._proc
        assert proc is not None
        assert proc.poll() is not None, "hung MCP subprocess was left as an orphan"
    finally:
        mgr.shutdown()


# --------------------------------------------------------- SSRAgent wiring
def test_agent_registers_and_routes_mcp_tools(settings):
    _write_config(settings.home)
    from ssr.agent.core import SSRAgent

    agent = SSRAgent(settings)
    try:
        # Tools discovered and seeded into the context pool.
        names = {t.qualified_name for t in agent.mcp_tools}
        assert names == {"mcp__echo__echo", "mcp__echo__add", "mcp__echo__snapshot"}
        spec_names = {s["name"] for s in agent.mcp_tool_specs}
        assert "mcp__echo__echo" in spec_names

        # A genai Tool of function declarations is built for the model.
        param = agent._mcp_tools_parameter()
        decl_names = {d.name for d in param.function_declarations}
        assert decl_names == names

        # Routing a call reaches the live server and returns its result.
        assert agent.mcp_manager.call_tool("mcp__echo__add", {"a": 10, "b": 5}) == "15"
    finally:
        agent.close()
