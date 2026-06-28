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


def test_miloco_tools_registered_in_toolkit():
    # The miloco tools must be exposed to the model.
    from ssr.agent.tools import ToolKit

    expected = {
        "miloco_status", "miloco_devices", "miloco_device_control",
        "miloco_family", "miloco_activities", "miloco_automations", "miloco_sync",
    }
    assert expected <= set(dir(ToolKit))
