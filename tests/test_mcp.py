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


# ----------------------------------------------------------- live subprocess
def test_manager_spawns_and_lists_tools(settings):
    path = _write_config(settings.home)
    mgr = MCPManager.from_config(path)
    try:
        tools = mgr.start_all()
        names = {t.qualified_name for t in tools}
        # disabled server is skipped; echo server exposes echo + add
        assert names == {"mcp__echo__echo", "mcp__echo__add"}
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


# --------------------------------------------------------- SSRAgent wiring
def test_agent_registers_and_routes_mcp_tools(settings):
    _write_config(settings.home)
    from ssr.agent.core import SSRAgent

    agent = SSRAgent(settings)
    try:
        # Tools discovered and seeded into the context pool.
        names = {t.qualified_name for t in agent.mcp_tools}
        assert names == {"mcp__echo__echo", "mcp__echo__add"}
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
