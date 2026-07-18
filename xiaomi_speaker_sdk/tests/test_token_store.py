from __future__ import annotations

from xiaomi_speaker_sdk import token_store


def test_save_and_load_round_trip(tmp_path):
    p = tmp_path / "token.json"
    token_store.save_token(p, {"a": 1, "b": "x"})
    assert token_store.load_token(p) == {"a": 1, "b": "x"}


def test_load_missing_file_returns_empty_dict(tmp_path):
    assert token_store.load_token(tmp_path / "missing.json") == {}


def test_import_pass_token_sets_fields_and_drops_stale_sid(tmp_path):
    p = tmp_path / "token.json"
    token_store.save_token(p, {"deviceId": "ABC123", "micoapi": ["s", "t"]})
    token_store.import_pass_token(p, "  the-pass-token  ", "12345")
    tok = token_store.load_token(p)
    assert tok["passToken"] == "the-pass-token"
    assert tok["userId"] == "12345"
    assert tok["deviceId"] == "ABC123"
    assert "micoapi" not in tok
    assert "xiaomi.smarthome" in tok["userAgent"]


def test_import_pass_token_generates_device_id_if_missing(tmp_path):
    p = tmp_path / "token.json"
    token_store.import_pass_token(p, "pt", "uid")
    tok = token_store.load_token(p)
    assert tok["deviceId"]
    assert tok["passToken"] == "pt"


def test_has_pass_token(tmp_path):
    p = tmp_path / "token.json"
    assert token_store.has_pass_token(p) is False
    token_store.import_pass_token(p, "pt", "uid")
    assert token_store.has_pass_token(p) is True
