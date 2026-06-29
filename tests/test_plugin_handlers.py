"""Tests for the refactored plugin system (config + agent tools + declared bus
handlers) and the new bus-handler action kinds (mcp_tool / shell / python)."""

from __future__ import annotations

from types import SimpleNamespace

from ssr.config import Settings


def _settings(tmp_path):
    s = Settings(home=tmp_path / "home", project_dir=tmp_path / "proj")
    s.ensure_dirs()
    return s


# ------------------------------------------------------------------- plugins
def test_openarm_builtin_plugin_discoverable_and_toggleable(tmp_path):
    from ssr import plugins

    s = _settings(tmp_path)
    rows = {p["name"]: p for p in plugins.list_plugins(s)}
    assert "openarm" in rows, "the bundled openarm plugin should be discoverable"
    assert rows["openarm"]["enabled"] is True
    assert rows["openarm"]["agent_tools"] == ["ssr.robotics.tools:ArmTools"]
    assert "ssr.robotics.tools:ArmTools" in plugins.plugin_agent_tools(s)

    plugins.set_plugin_enabled(s, "openarm", False)
    rows = {p["name"]: p for p in plugins.list_plugins(s)}
    assert rows["openarm"]["enabled"] is False
    assert "ssr.robotics.tools:ArmTools" not in plugins.plugin_agent_tools(s)

    plugins.set_plugin_enabled(s, "openarm", True)
    assert "ssr.robotics.tools:ArmTools" in plugins.plugin_agent_tools(s)


def test_plugin_declared_bus_handlers_collected(tmp_path):
    from ssr import plugins

    s = _settings(tmp_path)
    pdir = s.plugins_dir / "demo" / ".claude-plugin"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        '{"name": "demo", "handlers": ['
        '{"event": "sensor.motion", "kind": "shell", "command": "echo hi"},'
        '{"event": "task.*", "kind": "python", "code": "pass"}]}',
        "utf-8",
    )
    handlers = {h["event"]: h for h in plugins.plugin_bus_handlers(s)}
    assert handlers["sensor.motion"]["kind"] == "shell"
    assert handlers["task.*"]["kind"] == "python"

    plugins.set_plugin_enabled(s, "demo", False)
    assert all(h.get("_plugin") != "demo" for h in plugins.plugin_bus_handlers(s))


def test_toolkit_loads_arm_tools_via_plugin(tmp_path):
    from ssr.agent.tools import ToolKit

    tk = ToolKit(_settings(tmp_path), retriever=None, memory=None)
    names = {fn.__name__ for fn in tk.callables()}
    # arm_* tools arrive through the openarm plugin, not a hardcoded import
    assert {"arm_describe", "arm_invoke", "arm_report_done"} <= names
    assert {"bus_create_mcp_handler", "bus_create_shell_handler",
            "bus_create_python_handler"} <= names


# ---------------------------------------------------- handler-creation tools
def test_bus_create_handler_tools_build_correct_actions(tmp_path):
    from ssr.agent.tools import ToolKit

    tk = ToolKit(_settings(tmp_path), retriever=None, memory=None)
    captured = []
    tk.agent_instance = SimpleNamespace(
        create_bus_handler=lambda event, once=False, description="", action=None:
            captured.append((event, once, action)) or "hid",
    )
    tk.bus_create_mcp_handler("sensor.motion", "mcp__x__y", '{"a": 1}', "once")
    tk.bus_create_shell_handler("task.done", "echo hi")
    tk.bus_create_python_handler("foo.bar", "x = payload['n']")

    assert [a[2]["kind"] for a in captured] == ["mcp_tool", "shell", "python"]
    assert captured[0][2]["tool"] == "mcp__x__y" and captured[0][2]["args"] == {"a": 1}
    assert captured[0][1] is True  # once
    assert captured[1][2]["command"] == "echo hi" and captured[1][1] is False
    assert captured[2][2]["code"] == "x = payload['n']"


# ---------------------------------------------------- core action execution
def test_run_action_handler_executes_each_kind(tmp_path):
    from ssr.agent.core import SSRAgent
    from ssr.bus.events import BusEvent

    s = _settings(tmp_path)
    emitted, mcp_calls = [], []
    shim = SimpleNamespace(
        settings=s,
        mcp_manager=SimpleNamespace(call_tool=lambda t, a: mcp_calls.append((t, a)) or "ok"),
        toolkit=SimpleNamespace(run_command=lambda c: "ran"),
        bus=SimpleNamespace(),
        _emit=lambda *a, **k: emitted.append((a, k)),
    )
    ev = BusEvent(topic="t.x", payload={"n": 5}, source="src")

    SSRAgent._run_action_handler(
        shim, ev,
        {"kind": "python",
         "code": "open(str(settings.project_dir / 'out.txt'), 'w').write(str(payload['n']))"},
    )
    assert (s.project_dir / "out.txt").read_text() == "5"

    SSRAgent._run_action_handler(shim, ev, {"kind": "mcp_tool", "tool": "mcp__a__b", "args": {"k": 1}})
    assert mcp_calls == [("mcp__a__b", {"k": 1})]

    SSRAgent._run_action_handler(shim, ev, {"kind": "shell", "command": "echo hi"})
    assert emitted  # each kind emits a thinking line
