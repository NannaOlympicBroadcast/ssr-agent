"""Tests for the remote-control node client (filesystem RPCs, config, PTY).

These never touch the network: they exercise the RPC helper functions, config
serialisation, and a real local PTY session end to end.
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path

import pytest

from ssr.config import Settings
from ssr.integrations import remote


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    s = Settings(home=home, project_dir=proj, default_model="gemini-3.1-flash-lite")
    s.ensure_dirs()
    return s


# ----------------------------------------------------------------- config
def test_ws_url_derivation():
    assert remote.RemoteConfig("https://h:8787", "t", "n").ws_url() == "wss://h:8787/ws/node"
    assert remote.RemoteConfig("http://h:8787/", "t", "n").ws_url() == "ws://h:8787/ws/node"
    assert remote.RemoteConfig("h:8787", "t", "n").ws_url() == "ws://h:8787/ws/node"
    assert remote.RemoteConfig("wss://h/", "t", "n").ws_url() == "wss://h/ws/node"


def test_config_round_trip(settings):
    cfg = remote.RemoteConfig("http://localhost:8787", "sk-ssr-x", "box", ["dev", "gpu"])
    path = remote.save_config(settings, cfg)
    assert path.exists()
    loaded = remote.load_config(settings)
    assert loaded.endpoint == cfg.endpoint
    assert loaded.token == cfg.token
    assert loaded.tags == ["dev", "gpu"]


def test_load_config_missing(settings):
    assert remote.load_config(settings) is None


# ----------------------------------------------------------------- fs RPCs
def test_fs_list(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "sub").mkdir()
    res = remote._fs_list(str(tmp_path))
    names = {e["name"]: e for e in res["entries"]}
    assert names["sub"]["is_dir"] is True
    assert names["a.txt"]["is_dir"] is False
    assert names["a.txt"]["size"] == 2
    assert res["path"] == str(tmp_path)


def test_fs_list_missing_raises():
    with pytest.raises(FileNotFoundError):
        remote._fs_list("/no/such/path/xyz")


def test_fs_read_write_round_trip(tmp_path):
    dest = tmp_path / "nested" / "img.bin"
    payload = b"\x00\x01\x02hello\xff"
    res = remote._fs_write(str(dest), base64.b64encode(payload).decode())
    assert res["bytes"] == len(payload)
    assert dest.read_bytes() == payload

    read = remote._fs_read(str(dest))
    assert base64.b64decode(read["data_b64"]) == payload
    assert read["name"] == "img.bin"


def test_run_command(tmp_path):
    res = remote._run_command("echo hello-rc", str(tmp_path), 30)
    assert res["exit"] == 0
    assert "hello-rc" in res["output"]


def test_run_command_timeout():
    import sys
    cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    res = remote._run_command(cmd, "", 1)
    assert res["exit"] == -1
    assert "timed out" in res["output"]


# ----------------------------------------------------------------- PTY
@pytest.mark.skipif(not remote._HAS_PTY, reason="PTY not available on this platform")
def test_pty_session_echo(tmp_path):
    chunks: list[bytes] = []
    done = threading.Event()
    exit_code = {}

    def on_output(data: bytes) -> None:
        chunks.append(data)

    def on_exit(code: int) -> None:
        exit_code["code"] = code
        done.set()

    sess = remote.PtySession(str(tmp_path), on_output, on_exit)
    sess.start()
    sess.write("echo pty-works\n")
    sess.write("exit\n")
    assert done.wait(timeout=10), "shell did not exit in time"
    output = b"".join(chunks).decode("utf-8", errors="replace")
    assert "pty-works" in output
