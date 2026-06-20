"""Regression tests for the XiaoAI (小爱音箱) login / re-login logic.

These cover the fix for the channel's login silently expiring: when a cached
``serviceToken`` is rejected, ``mi_request`` must invalidate it and drive a
fresh login (mirroring Xiaoai-Claw-Addon's ``invalidateSid`` + ``login``),
instead of reloading and trusting the dead token forever.
"""

from __future__ import annotations

import asyncio

import pytest

from ssr.integrations import xiaomi


class FakeStore:
    def __init__(self, token=None):
        self.saved = dict(token) if token else token

    def load_token(self):
        return self.saved

    def save_token(self, token=None):
        self.saved = dict(token) if token else token


def _make_account(token):
    acct = xiaomi.CustomMiAccount.__new__(xiaomi.CustomMiAccount)
    acct.token = token
    acct.token_store = FakeStore(token)
    acct.now_ua = None
    acct.username = "user"
    acct.password = "pass"
    return acct


def test_account_user_agent_is_stable_and_app_like():
    ua = xiaomi._account_user_agent("DEADBEEF")
    assert "xiaomi.smarthome" in ua
    assert "DEADBEEF" in ua
    # Deterministic for a given deviceId.
    assert ua == xiaomi._account_user_agent("DEADBEEF")


def test_is_auth_error_matches_expiry_but_not_network():
    assert xiaomi._is_auth_error(Exception("Error url: {'code': 401, 'message': 'auth'}"))
    assert xiaomi._is_auth_error(Exception("HTTP 401 Unauthorized"))
    assert xiaomi._is_auth_error(Exception("login failed"))
    assert not xiaomi._is_auth_error(Exception("Connection reset by peer"))
    assert not xiaomi._is_auth_error(Exception("Cannot connect to host"))


def test_invalidate_sid_drops_servicetoken_but_keeps_passtoken():
    token = {
        "deviceId": "D",
        "userId": "U",
        "passToken": "PASS",
        "micoapi": ("ssec", "STALE"),
    }
    acct = _make_account(token)
    acct._invalidate_sid("micoapi")
    assert "micoapi" not in acct.token
    # The long-lived credentials survive so the next login can refresh silently.
    assert acct.token["passToken"] == "PASS"
    assert acct.token["deviceId"] == "D"
    assert acct.token_store.saved.get("micoapi") is None
    assert acct.token_store.saved.get("passToken") == "PASS"


def test_mi_request_relogins_on_expired_servicetoken(monkeypatch):
    token = {
        "deviceId": "D",
        "userId": "U",
        "passToken": "PASS",
        "micoapi": ("ssec", "STALE"),
    }
    acct = _make_account(token)
    calls = {"super": 0, "login": 0}

    async def fake_super(self, sid, url, data, headers, relogin=True):
        calls["super"] += 1
        # A stale serviceToken is rejected just like the real Mi cloud does.
        if self.token.get("micoapi", (None, None))[1] == "STALE":
            raise Exception(f"Error {url}: {{'code': 401, 'message': 'auth error'}}")
        return {"code": 0, "ok": True}

    monkeypatch.setattr(xiaomi.MiAccount, "mi_request", fake_super, raising=False)

    async def fake_login(sid):
        calls["login"] += 1
        acct.token[sid] = ("ssec", "FRESH")
        acct.token_store.save_token(acct.token)
        return True

    acct.login = fake_login  # type: ignore[assignment]

    result = asyncio.run(acct.mi_request("micoapi", "http://x", None, {}))

    assert result == {"code": 0, "ok": True}
    assert calls["login"] == 1               # re-login happened exactly once
    assert calls["super"] == 2               # first (fail) + retry (ok)
    assert acct.token["micoapi"][1] == "FRESH"


def test_mi_request_does_not_relogin_on_network_error(monkeypatch):
    token = {"deviceId": "D", "userId": "U", "passToken": "P", "micoapi": ("s", "T")}
    acct = _make_account(token)
    calls = {"login": 0}

    async def fake_super(self, sid, url, data, headers, relogin=True):
        raise Exception("Connection reset by peer")

    monkeypatch.setattr(xiaomi.MiAccount, "mi_request", fake_super, raising=False)

    async def fake_login(sid):
        calls["login"] += 1
        return True

    acct.login = fake_login  # type: ignore[assignment]

    with pytest.raises(Exception, match="Connection reset"):
        asyncio.run(acct.mi_request("micoapi", "http://x", None, {}))
    assert calls["login"] == 0               # transient errors must not re-login
