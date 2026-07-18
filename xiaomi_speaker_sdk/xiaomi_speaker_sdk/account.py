"""A ``miservice`` ``MiAccount`` subclass with a working re-login path.

Mirrors the login hardening in ssr-agent's XiaoAI channel (itself modeled on
https://github.com/ZhengXieGang/Xiaoai-Claw-Addon): a stable, app-like
User-Agent derived from the cached ``deviceId`` (fewer security challenges
from a server / new IP), and a re-login path that actually invalidates an
expired ``serviceToken`` instead of silently reloading and trusting it.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("xiaomi_speaker_sdk")

try:
    from miservice import MiAccount
except ImportError:  # pragma: no cover - surfaced by speaker.py with a clear error
    class MiAccount:  # type: ignore[no-redef]
        pass


def account_user_agent(device_id: str) -> str:
    """Stable Mi-smarthome Android User-Agent keyed on the deviceId."""
    return (
        f"Android-7.1.1-1.0.0-ONEPLUS A3010-136-{device_id} "
        "APP/xiaomi.smarthome APPV/62830"
    )


def is_auth_error(exc: Exception) -> bool:
    """Whether an exception looks like an expired / invalid login."""
    msg = str(exc).lower()
    return (
        "401" in msg
        or "auth" in msg
        or "login failed" in msg
        or "unauthorized" in msg
        or "'code': 3" in msg
        or '"code": 3' in msg
    )


_SENSITIVE_KEYS = {
    "passToken", "password", "hash", "ssecurity", "psecurity", "serviceToken",
    "cUserId", "_sign", "nonce",
}


def _redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _SENSITIVE_KEYS and v:
                out[k] = f"<redacted len={len(str(v))}>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


class CustomMiAccount(MiAccount):
    def __init__(self, session, username, password, token_store=None):
        super().__init__(session, username, password, token_store)
        if self.token:
            user_agent = self.token.get("userAgent")
            if user_agent:
                self.now_ua = user_agent

    async def _serviceLogin(self, uri, data=None):
        device_id = self.token["deviceId"]
        user_agent = account_user_agent(device_id)
        if self.token.get("userAgent") != user_agent:
            self.token["userAgent"] = user_agent
            if self.token_store:
                self.token_store.save_token(self.token)

        self.now_ua = user_agent
        headers = {"User-Agent": user_agent}
        cookies = {"sdkVersion": "3.9", "deviceId": device_id}
        if "passToken" in self.token:
            cookies["userId"] = self.token["userId"]
            cookies["passToken"] = self.token["passToken"]
        else:
            cookies["passToken"] = ""
        url = "https://account.xiaomi.com/pass/" + uri
        method = "GET" if data is None else "POST"
        self._last_request = {
            "method": method,
            "url": url,
            "cookies": {
                "deviceId": device_id,
                "userId": cookies.get("userId", ""),
                "passToken": ("<set>" if cookies.get("passToken") else "<empty>"),
                "sdkVersion": cookies.get("sdkVersion"),
            },
            "data": _redact(dict(data)) if isinstance(data, dict) else data,
            "userAgent": user_agent,
        }
        async with self.session.request(
            method, url, data=data, cookies=cookies, headers=headers, ssl=False,
        ) as r:
            raw = await r.read()
            status = r.status
        resp = json.loads(raw[11:])
        logger.debug(
            "Mi %s %s -> HTTP %s | request=%s | response=%s",
            method, uri, status, self._last_request, _redact(resp),
        )
        return resp

    async def login(self, sid):
        import hashlib

        if not self.token:
            self.token = self.token_store.load_token() if self.token_store else None
            if not self.token:
                self.token = {}

        if not self.token.get("deviceId"):
            try:
                from miservice.miaccount import get_random
            except ImportError:
                def get_random(length):
                    import random
                    import string

                    return "".join(random.sample(string.ascii_letters + string.digits, length))
            self.token["deviceId"] = get_random(16).upper()

        self.token["userAgent"] = account_user_agent(self.token["deviceId"])
        device_id = self.token.get("deviceId")
        user_agent = self.token.get("userAgent")
        self.now_ua = user_agent

        if self.token_store and device_id and user_agent:
            self.token_store.save_token({"deviceId": device_id, "userAgent": user_agent})

        if self.token and sid in self.token and self.token.get("userId") and self.token.get("passToken"):
            return True

        try:
            resp = await self._serviceLogin(f"serviceLogin?sid={sid}&_json=true")
            if resp["code"] != 0:
                data = {
                    "_json": "true",
                    "qs": resp["qs"],
                    "sid": resp["sid"],
                    "_sign": resp["_sign"],
                    "callback": resp["callback"],
                    "user": self.username,
                    "hash": hashlib.md5(self.password.encode()).hexdigest().upper(),
                }
                resp = await self._serviceLogin("serviceLoginAuth2", data)

            if (resp.get("code") != 0 or
                    resp.get("notificationUrl") or
                    resp.get("captchaUrl") or
                    ("userId" not in resp and not self.token.get("userId"))):
                raise Exception(resp)

            if "userId" in resp:
                self.token["userId"] = resp["userId"]
            if "passToken" in resp:
                self.token["passToken"] = resp["passToken"]

            serviceToken = await self._securityTokenService(
                resp["location"], resp["nonce"], resp["ssecurity"]
            )
            self.token[sid] = (resp["ssecurity"], serviceToken)
            if self.token_store:
                self.token_store.save_token(self.token)
            return True

        except Exception as e:
            self.token = {"deviceId": device_id, "userAgent": user_agent}
            if self.token_store:
                self.token_store.save_token(self.token)

            resp_dict = e.args[0] if (e.args and isinstance(e.args[0], dict)) else None
            if resp_dict is not None:
                reasons = []
                if resp_dict.get("code") not in (0, None):
                    reasons.append(f"code={resp_dict.get('code')}")
                if resp_dict.get("notificationUrl"):
                    reasons.append("notificationUrl set (needs verification)")
                if resp_dict.get("captchaUrl"):
                    reasons.append("captchaUrl set (needs captcha)")
                if "userId" not in resp_dict and not self.token.get("userId"):
                    reasons.append("no userId in response")
                logger.error(
                    "MiAccount login(%s) failed | failing-check=[%s] | request=%s | response=%s",
                    sid, ", ".join(reasons) or "unknown",
                    getattr(self, "_last_request", None), _redact(resp_dict),
                )
            else:
                logger.error(
                    "MiAccount login(%s) errored: %s | request=%s",
                    sid, e, getattr(self, "_last_request", None),
                )
            return False

    def _invalidate_sid(self, sid):
        """Drop the cached ``(ssecurity, serviceToken)`` for *sid* while keeping
        the long-lived passToken / deviceId, so the next login refreshes silently.
        """
        if not self.token:
            self.token = (self.token_store.load_token() if self.token_store else None) or {}
        removed = self.token.pop(sid, None) is not None
        if self.token_store:
            self.token_store.save_token(self.token)
        if removed:
            logger.info("invalidated cached serviceToken for sid=%s", sid)

    async def mi_request(self, sid, url, data, headers, relogin=True):
        device_id = self.token.get("deviceId") if self.token else None
        user_agent = self.token.get("userAgent") if self.token else None
        had_sid_token = bool(self.token and sid in self.token)
        try:
            return await super().mi_request(sid, url, data, headers, relogin=False)
        except Exception as e:
            if relogin and had_sid_token and is_auth_error(e):
                logger.info("serviceToken for sid=%s rejected (expired); re-logging in…", sid)
                self._invalidate_sid(sid)
                if await self.login(sid):
                    logger.info("re-login OK for sid=%s; retrying request", sid)
                    return await super().mi_request(sid, url, data, headers, relogin=False)
                logger.warning("re-login failed for sid=%s", sid)
            if self.token is None and device_id and user_agent:
                self.token = {"deviceId": device_id, "userAgent": user_agent}
                if self.token_store:
                    self.token_store.save_token(self.token)
            raise


async def diagnose_login(account, username: str, password: str) -> tuple[str | None, str]:
    """Replay the login to surface *why* it failed (verification URL / bad password)."""
    import hashlib

    generic = (
        "Mi account login failed. Common causes: wrong account/password, wrong "
        "region, or the account triggered safety verification."
    )
    try:
        from miservice.miaccount import get_random
    except Exception:
        get_random = None
    try:
        existing_device_id = account.token.get("deviceId") if account.token else None
        if not existing_device_id:
            existing_device_id = (get_random(16).upper() if get_random else "0123456789ABCDEF")
        account.token = {"deviceId": existing_device_id}
        resp = await account._serviceLogin("serviceLogin?sid=micoapi&_json=true")
        if resp.get("code") != 0:
            data = {
                "_json": "true",
                "qs": resp.get("qs"),
                "sid": resp.get("sid"),
                "_sign": resp.get("_sign"),
                "callback": resp.get("callback"),
                "user": username,
                "hash": hashlib.md5(password.encode()).hexdigest().upper(),
            }
            resp = await account._serviceLogin("serviceLoginAuth2", data)
    except Exception as e:
        logger.exception("login diagnosis failed")
        return None, f"{generic}\n(diagnosis also errored: {e})"

    code = resp.get("code")
    desc = resp.get("desc") or resp.get("description") or ""
    logger.error("login diagnosis: code=%s desc=%s keys=%s", code, desc, list(resp.keys()))

    notif = resp.get("notificationUrl")
    if notif:
        if notif.startswith("/"):
            notif = "https://account.xiaomi.com" + notif
        reason = (
            "The Mi account needs safety verification (common when logging in "
            "from a server / new IP). Open this URL in a browser to complete it "
            "(SMS / app confirm):\n\n"
            f"{notif}\n\n"
            "Prefer xiaomi_speaker_sdk.browser_auth.extract_token(), which drives "
            "a real browser through this automatically."
        )
        return notif, reason
    if resp.get("captchaUrl"):
        cap = resp["captchaUrl"]
        if cap.startswith("/"):
            cap = "https://account.xiaomi.com" + cap
        reason = f"Mi login needs a captcha. Complete it once in a browser:\n    {cap}"
        return cap, reason
    if code == 70016 or "password" in desc.lower():
        return None, f"Wrong Mi account or password (code={code} {desc})."
    if "userId" not in resp:
        return None, (
            f"Mi login did not return a userId (code={code} {desc}). Usually "
            "means safety verification is required, or bad credentials."
        )
    return None, f"{generic}\n(code={code} {desc})"
