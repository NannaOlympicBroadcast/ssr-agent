"""Tests for the conversation SessionStore and the run_command decode fix."""

from __future__ import annotations

from pathlib import Path

import pytest

from ssr.agent.sessions import SessionStore
from ssr.agent.tools import decode_output
from ssr.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    s = Settings(home=home, project_dir=proj)
    s.ensure_dirs()
    return s


# ----------------------------------------------------------------- sessions
def test_create_append_and_get(settings):
    store = SessionStore(settings)
    sid = store.create(title="first message about pasta", cwd="/tmp/x")
    store.append(sid, "user", "hello")
    store.append(sid, "assistant", "hi there")
    data = store.get(sid)
    assert data["id"] == sid
    assert data["cwd"] == "/tmp/x"
    assert data["title"].startswith("first message")
    assert [t["role"] for t in data["turns"]] == ["user", "assistant"]
    assert data["turns"][1]["content"] == "hi there"


def test_list_orders_newest_first_and_counts(settings):
    store = SessionStore(settings)
    s1 = store.create(title="one")
    store.append(s1, "user", "a")
    s2 = store.create(title="two")
    store.append(s2, "user", "b")
    store.append(s2, "assistant", "c")
    listing = store.list()
    ids = [s["id"] for s in listing]
    assert set(ids) == {s1, s2}
    # newest (s2) first; turn counts surfaced
    by_id = {s["id"]: s for s in listing}
    assert by_id[s2]["turns"] == 2
    assert by_id[s1]["turns"] == 1


def test_title_derived_from_first_user_turn_when_empty(settings):
    store = SessionStore(settings)
    sid = store.create(title="", cwd="")
    store.append(sid, "user", "please refactor the parser")
    meta = next(s for s in store.list() if s["id"] == sid)
    assert "refactor the parser" in meta["title"]


def test_delete(settings):
    store = SessionStore(settings)
    sid = store.create(title="x")
    assert store.delete(sid) is True
    assert store.get(sid) is None
    assert store.delete(sid) is False


def test_get_missing_returns_none(settings):
    assert SessionStore(settings).get("nope") is None


def test_path_traversal_is_neutralised(settings):
    store = SessionStore(settings)
    # A malicious id must not escape the sessions directory.
    assert store.get("../../etc/passwd") is None


# ------------------------------------------------------------- decode_output
def test_decode_output_utf8():
    assert decode_output("héllo 谢谢喵".encode("utf-8")) == "héllo 谢谢喵"


def test_decode_output_gbk_does_not_raise():
    # Bytes that are invalid UTF-8 (e.g. GBK-encoded Chinese) must still decode.
    raw = "目录".encode("gbk")
    out = decode_output(raw)
    assert isinstance(out, str) and out  # no exception, non-empty


def test_decode_output_arbitrary_bytes_never_raise():
    assert isinstance(decode_output(b"\xff\xfe\x00\xae random"), str)
    assert decode_output(b"") == ""
    assert decode_output(None) == ""
