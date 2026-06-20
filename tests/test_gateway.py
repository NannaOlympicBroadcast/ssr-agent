"""Tests for the gateway (channel-as-system-service) integration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ssr.config import Settings
from ssr.integrations import gateway as gw


def _settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    home.mkdir()
    return Settings(home=home, project_dir=tmp_path)


def test_gateway_record_roundtrip(tmp_path):
    settings = _settings(tmp_path)
    record = gw.Gateway(name="box", channel="xiaomi", cwd="/srv/app", env={"FOO": "1"})
    gw.add_gateway(settings, record)

    loaded = gw.load_gateways(settings)
    assert "box" in loaded
    assert loaded["box"].channel == "xiaomi"
    assert loaded["box"].cwd == "/srv/app"
    assert loaded["box"].env == {"FOO": "1"}

    assert gw.get_gateway(settings, "box") == record
    assert gw.remove_gateway(settings, "box") is True
    assert gw.get_gateway(settings, "box") is None
    assert gw.remove_gateway(settings, "box") is False


def test_from_dict_ignores_unknown_keys():
    rec = gw.Gateway.from_dict({"name": "g", "channel": "feishu", "bogus": 1})
    assert rec.name == "g"
    assert rec.channel == "feishu"
    assert not hasattr(rec, "bogus")


def test_exec_args_uses_module_invocation():
    args = gw._exec_args("box")
    assert args[0] == sys.executable
    assert args[1:] == ["-m", "ssr", "gateway", "run", "box"]


def test_systemd_unit_text_is_well_formed(tmp_path):
    settings = _settings(tmp_path)
    record = gw.Gateway(name="box", channel="all", cwd="/work", env={"K": "V"})
    text = gw.SystemdManager()._unit_text(settings, record)
    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    assert "Restart=always" in text
    assert f"Environment=SSR_HOME={settings.home}" in text
    assert "Environment=K=V" in text
    assert "WorkingDirectory=/work" in text
    assert "-m ssr gateway run box" in text
    assert "WantedBy=default.target" in text


def test_launchd_plist_is_valid_xml(tmp_path):
    import plistlib

    settings = _settings(tmp_path)
    record = gw.Gateway(name="box", channel="feishu", cwd="/work")
    text = gw.LaunchdManager()._plist_text(settings, record)
    parsed = plistlib.loads(text.encode("utf-8"))
    assert parsed["Label"] == "com.ssr.gateway.box"
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["ProgramArguments"][-3:] == ["gateway", "run", "box"]
    assert parsed["EnvironmentVariables"]["SSR_HOME"] == str(settings.home)


def test_windows_wrapper_script(tmp_path):
    settings = _settings(tmp_path)
    record = gw.Gateway(name="box", channel="wechat", cwd="C:/work", env={"K": "V"})
    path = gw.WindowsTaskManager()._write_wrapper(settings, record)
    body = path.read_text("utf-8")
    assert "@echo off" in body
    assert "set \"SSR_HOME=" in body
    assert "set \"K=V\"" in body
    assert "cd /d \"C:/work\"" in body
    assert "gateway run box" in body


def test_null_manager_degrades_gracefully(tmp_path):
    settings = _settings(tmp_path)
    record = gw.Gateway(name="box", channel="xiaomi")
    mgr = gw._NullManager()
    assert mgr.available() is False
    msg = mgr.install(settings, record)
    assert "gateway run box" in msg
    assert mgr.status(settings, "box") == "unknown (no service manager)"


def test_get_manager_matches_platform(monkeypatch):
    monkeypatch.setattr(gw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gw.SystemdManager, "available", lambda self: True)
    assert isinstance(gw.get_manager(), gw.SystemdManager)

    monkeypatch.setattr(gw.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(gw.LaunchdManager, "available", lambda self: True)
    assert isinstance(gw.get_manager(), gw.LaunchdManager)

    monkeypatch.setattr(gw.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gw.WindowsTaskManager, "available", lambda self: True)
    assert isinstance(gw.get_manager(), gw.WindowsTaskManager)

    # Unsupported / unavailable → NullManager.
    monkeypatch.setattr(gw.platform, "system", lambda: "Plan9")
    assert isinstance(gw.get_manager(), gw._NullManager)


def test_run_gateway_missing_record_errors(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(SystemExit):
        gw.run_gateway(settings, "nope")
