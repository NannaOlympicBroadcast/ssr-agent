"""Tests for the Xiaomi Miloco integration (replacement for the miot plugin)."""

import json
import tempfile
from pathlib import Path

from ssr.config import Settings
from ssr.integrations import miloco as ml


def _settings(tmp: Path) -> Settings:
    s = Settings(home=tmp / "home", project_dir=tmp / "project")
    s.ensure_dirs()
    return s


def test_config_roundtrip_and_endpoint_defaults():
    with tempfile.TemporaryDirectory() as d:
        s = _settings(Path(d))
        cfg = ml.load_config(s)
        # Partial endpoint override merges onto the defaults.
        cfg.endpoints = {"devices": "/custom/devices"}
        cfg.base_url = "http://example:1810"
        ml.save_config(s, cfg)
        again = ml.load_config(s)
        assert again.base_url == "http://example:1810"
        assert again.endpoints["devices"] == "/custom/devices"
        # Untouched endpoints still resolve from the defaults.
        assert again.endpoints["activities"] == ml.DEFAULT_ENDPOINTS["activities"]


def test_env_overrides_and_token_discovery(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        s = _settings(Path(d))
        # A config file says one base_url...
        ml.save_config(s, ml.MilocoConfig(base_url="http://file:1810"))
        # ...but env is authoritative (containerized deploy points at the service).
        monkeypatch.setenv("MILOCO_BASE_URL", "http://miloco:1810")
        monkeypatch.setenv("MILOCO_TOKEN", "tok-123")
        cfg = ml.load_config(s)
        assert cfg.base_url == "http://miloco:1810"
        assert cfg.api_key == "tok-123"

        # Token auto-discovery from a shared Miloco config.json (server.token).
        monkeypatch.delenv("MILOCO_TOKEN", raising=False)
        monkeypatch.delenv("MILOCO_API_KEY", raising=False)
        mlc_cfg = Path(d) / "miloco-config.json"
        mlc_cfg.write_text(json.dumps({"server": {"token": "auto-tok"}}), "utf-8")
        monkeypatch.setenv("MILOCO_CONFIG_FILE", str(mlc_cfg))
        cfg2 = ml.load_config(s)
        assert cfg2.api_key == "auto-tok"


def test_normal_response_envelope_unwrapping():
    f = ml.MilocoClient._as_list
    assert f({"code": 0, "data": {"events": [{"id": 1}]}}) == [{"id": 1}]
    assert f({"code": 0, "data": {"devices": [{"did": "a"}]}}) == [{"did": "a"}]
    assert f({"data": [{"x": 1}]}) == [{"x": 1}]
    assert f([{"x": 1}]) == [{"x": 1}]
    assert f(None) == []
    assert f({"code": 0, "data": {}}) == []


def test_windows_guard(monkeypatch):
    monkeypatch.setattr(ml.platform, "system", lambda: "Windows")
    monkeypatch.setattr(ml, "running_in_docker", lambda: False)
    assert ml.is_native_supported() is False
    try:
        ml.ensure_native_supported()
        assert False, "expected MilocoUnavailable on Windows"
    except ml.MilocoUnavailable as e:
        assert "Docker" in str(e)
    # Inside a container the guard passes.
    monkeypatch.setattr(ml, "running_in_docker", lambda: True)
    ml.ensure_native_supported()


def test_activity_helpers_and_topic():
    act = {"id": "evt-1", "type": "person.arrived", "timestamp": 1700000000000}
    assert ml._activity_id(act) == "evt-1"
    assert ml._activity_type(act) == "person_arrived"
    # Missing id falls back to a content key (still stable for de-dup).
    assert ml._activity_id({"timestamp": 1, "name": "x"})


def test_bridge_dedup_and_publish():
    with tempfile.TemporaryDirectory() as d:
        s = _settings(Path(d))
        published = []
        bridge = ml.MilocoActivityBridge(s, lambda t, p: published.append((t, p)))
        # Stub the client to return a fixed batch.
        batch = [
            {"id": "a", "type": "motion", "timestamp": 1000},
            {"id": "b", "type": "hazard.smoke", "timestamp": 2000},
        ]
        bridge.client.activities = lambda since_ms=None, limit=None: batch
        assert bridge.poll_once() == 2
        # Second poll: same ids → nothing new (de-dup).
        assert bridge.poll_once() == 0
        topics = [t for t, _ in published]
        assert "miloco.activity.motion" in topics
        assert "miloco.activity.hazard_smoke" in topics
        # State persisted across a fresh bridge instance.
        bridge2 = ml.MilocoActivityBridge(s, lambda t, p: published.append((t, p)))
        bridge2.client.activities = lambda since_ms=None, limit=None: batch
        assert bridge2.poll_once() == 0


def test_sse_event_handling_and_dedup():
    with tempfile.TemporaryDirectory() as d:
        s = _settings(Path(d))
        published = []
        bridge = ml.MilocoActivityBridge(s, lambda t, p: published.append((t, p)))
        # A bare event record frame.
        bridge._handle_sse_event("new_event", json.dumps(
            {"id": "e1", "type": "person.arrived", "timestamp": 1000}))
        # A wrapped frame ({"event": {...}}).
        bridge._handle_sse_event("new_event", json.dumps(
            {"event": {"id": "e2", "type": "hazard.smoke", "timestamp": 2000}}))
        # Duplicate id → ignored.
        bridge._handle_sse_event("new_event", json.dumps({"id": "e1", "type": "x"}))
        # Non-event frame → ignored.
        bridge._handle_sse_event("ping", "{}")
        topics = [t for t, _ in published]
        assert topics == ["miloco.activity.person_arrived", "miloco.activity.hazard_smoke"]
        assert "events_stream" in ml.DEFAULT_ENDPOINTS


def test_snapshot_and_context_loader():
    from ssr.context_pool.loaders import load_miloco_context

    with tempfile.TemporaryDirectory() as d:
        s = _settings(Path(d))
        snap = {
            "synced_at": 1.0,
            "base_url": "http://x:1810",
            "devices": [{"did": "1", "name": "Lamp"}],
            "members": [{"id": "p1", "name": "Alice"}],
            "activities": [{"id": "e1", "type": "motion"}],
            "automations": [{"rule_id": "r1", "name": "Night"}],
            "homes": [],
        }
        ml.snapshot_path(s).write_text(json.dumps(snap), "utf-8")
        items = load_miloco_context(s)
        titles = [i.title for i in items]
        assert any("snapshot" in t for t in titles)
        assert any("Lamp" in t for t in titles)
        assert any("Alice" in t for t in titles)
        assert all(i.metadata.get("kind") == "miloco" for i in items)


def test_miloco_tools_exposed_via_plugin():
    # Mi Home control is now the bundled `miloco` plugin (agent_tools ->
    # MilocoTools), not hardcoded on ToolKit — but the tools must still reach the
    # model through ToolKit.callables().
    from ssr.agent.tools import ToolKit
    from ssr.agent.tools_miloco import MilocoTools

    expected = {
        "miloco_status", "miloco_devices", "miloco_device_control",
        "miloco_device_status", "miloco_device_spec", "miloco_trigger_scene",
        "miloco_cameras", "miloco_family", "miloco_activities",
        "miloco_automations", "miloco_tasks", "miloco_home_profile",
        "miloco_scope", "miloco_notify", "miloco_refresh", "miloco_sync",
    }
    # MilocoTools advertises exactly these.
    assert {fn.__name__ for fn in MilocoTools(None).callables()} == expected

    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(home=Path(tmpdir) / "home", project_dir=Path(tmpdir) / "proj")
        settings.ensure_dirs()
        tk = ToolKit(settings, retriever=None, memory=None)
        names = {fn.__name__ for fn in tk.callables()}
        # ...and they arrive in the toolkit via the enabled miloco plugin.
        assert expected <= names
        # disabling the plugin removes them
        from ssr import plugins
        plugins.set_plugin_enabled(settings, "miloco", False)
        names_off = {fn.__name__ for fn in ToolKit(settings, retriever=None, memory=None).callables()}
        assert not (expected & names_off)


def test_miloco_builtin_plugin_manifest():
    from ssr import plugins
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(home=Path(tmpdir) / "home", project_dir=Path(tmpdir) / "proj")
        settings.ensure_dirs()
        rows = {p["name"]: p for p in plugins.list_plugins(settings)}
        assert "miloco" in rows
        assert rows["miloco"]["agent_tools"] == ["ssr.agent.tools_miloco:MilocoTools"]


def test_miloco_skills_bundled():
    # The Miloco capability skills are bundled as the agent's knowledge base.
    from pathlib import Path
    import ssr

    skills_root = Path(ssr.__file__).resolve().parent / "builtin_skills"
    bundled = {p.parent.name for p in skills_root.glob("miloco*/SKILL.md")}
    # A representative spread across Miloco's capability areas + our adapter.
    for name in ("miloco-overview", "miloco-devices", "miloco-notify",
                 "miloco-home-profile", "miloco-miot-scope", "miloco-create-task"):
        assert name in bundled, name


def test_control_payload_normalization():
    from ssr.agent.tools_miloco import _normalize_control

    # Shorthand siid/piid → set_property with prop.{siid}.{piid} iid.
    assert _normalize_control({"siid": 2, "piid": 1, "value": True}) == {
        "type": "set_property", "iid": "prop.2.1", "value": True}
    # Shorthand siid/aiid → call_action.
    assert _normalize_control({"siid": 5, "aiid": 1, "params": ["hi"]}) == {
        "type": "call_action", "iid": "action.5.1", "params": ["hi"]}
    # iid+value shorthand.
    assert _normalize_control({"iid": "prop.2.1", "value": 3})["type"] == "set_property"
    # Already a full request → unchanged.
    full = {"type": "set_properties", "properties": [{"iid": "prop.2.1", "value": 1}]}
    assert _normalize_control(full) == full


def test_new_endpoints_present():
    eps = ml.DEFAULT_ENDPOINTS
    for name in ("device_status", "device_spec", "scene_trigger", "tasks",
                 "home_profile", "scope_homes", "scope_cameras", "send_notify",
                 "cameras", "refresh_all"):
        assert name in eps, name
    # The non-existent GET /scenes was removed (only POST trigger exists).
    assert "scenes" not in eps
