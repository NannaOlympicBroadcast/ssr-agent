"""Xiaomi (小爱音箱 / XiaoAI speaker) cloud integration.

Lets SSR talk *as* a XiaoAI speaker: it polls the Mi cloud for what the user
said (ASR) and replies with the speaker's text-to-speech. The mechanism mirrors
the reference plugin https://github.com/ZhengXieGang/Xiaoai-Claw-Addon :

* **Input (ASR)** — poll the speaker's latest recognised query via the Mi
  ``ubus`` ``nlp_result_get`` call (``MiNAService.get_latest_ask``), which returns
  the user's spoken question plus a request id / timestamp.
* **Reply (TTS)** — speak text back through the speaker with
  ``ubus text_to_speech`` (``MiNAService.text_to_speech``).

The heavy lifting (Mi passport login, signing, token cache) is delegated to the
``miservice`` package. Credentials live in ``~/.ssr/xiaomi.json`` and are
**shared with the bundled ``miot`` plugin** (MiService convention).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Settings

logger = logging.getLogger("ssr.xiaomi")


# --------------------------------------------------------------------- config
@dataclass
class XiaomiConfig:
    account: str = ""               # Mi account (id / email / phone)
    password: str = ""
    server_country: str = "cn"      # cn / de / i2 / ru / sg / us
    did: str = ""                   # Mi Home device DID
    minaDeviceId: str = ""          # MiNA cloud deviceID (preferred selector)
    speakerName: str = ""           # device name in Mi Home, to disambiguate
    hardware: str = ""              # e.g. L05C
    wake_word: str = ""             # optional: only forward queries containing it
    default_cwd: str = ""           # starting working directory (not a hard limit)
    # Used by the bundled ``miot`` plugin's ${xiaomi.*} placeholders:
    miot_mcp_command: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "XiaomiConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def config_path(settings: Settings) -> Path:
    # Shared with the bundled ``miot`` plugin (credentials namespace "xiaomi").
    return settings.home / "xiaomi.json"


def token_path(settings: Settings) -> Path:
    return settings.home / "xiaomi-token.json"


def load_config(settings: Settings) -> XiaomiConfig | None:
    p = config_path(settings)
    if not p.exists():
        return None
    try:
        return XiaomiConfig.from_dict(json.loads(p.read_text("utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def save_config(settings: Settings, cfg: XiaomiConfig) -> Path:
    p = config_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), "utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def configure_interactive(settings: Settings) -> XiaomiConfig:
    """Prompt for the Mi account credentials and speaker selection."""
    existing = load_config(settings) or XiaomiConfig()

    def ask(label: str, current: str = "", secret: bool = False) -> str:
        suffix = f" [{'•' * 6 if secret and current else current}]" if current else ""
        val = input(f"{label}{suffix}: ").strip()
        return val or current

    print("\n配置小爱音箱 (XiaoAI speaker)\n" + "-" * 32)
    cfg = XiaomiConfig(
        account=ask("小米账号 Mi account", existing.account),
        password=ask("密码 password", existing.password, secret=True),
        server_country=ask("区域 region (cn/de/i2/ru/sg/us)", existing.server_country or "cn"),
        minaDeviceId=ask("小爱 deviceID (留空可自动选择)", existing.minaDeviceId),
        speakerName=ask("设备名 speaker name (用于多设备时定位)", existing.speakerName),
        hardware=ask("硬件型号 hardware (可留空)", existing.hardware),
        wake_word=ask("唤醒词 wake word (留空=转发所有语音)", existing.wake_word),
        default_cwd=ask("默认工作目录 default cwd (可留空)", existing.default_cwd),
        did=existing.did,
        miot_mcp_command=existing.miot_mcp_command,
    )
    path = save_config(settings, cfg)
    print(f"\n已保存到 {path} (与 miot 插件共享凭据)")
    return cfg


# ----------------------------------------------------------- miservice bridge
class XiaomiUnavailable(RuntimeError):
    """Raised when miservice / aiohttp is not installed."""


try:
    from miservice import MiAccount
except ImportError:
    class MiAccount:  # type: ignore[no-redef]
        pass


def _account_user_agent(device_id: str) -> str:
    """Stable Mi-smarthome Android User-Agent keyed on the deviceId.

    Mirrors Xiaoai-Claw-Addon's ``buildAccountUserAgent``. A consistent,
    app-like UA (instead of a random browser string) markedly reduces the
    security-verification challenges that otherwise invalidate the login when
    running from a server / new IP.
    """
    return (
        f"Android-7.1.1-1.0.0-ONEPLUS A3010-136-{device_id} "
        "APP/xiaomi.smarthome APPV/62830"
    )


def _is_auth_error(exc: Exception) -> bool:
    """Whether an exception looks like an expired / invalid login.

    Mirrors the reference's ``isAuthErrorPayload`` (code 3 / 401 / an "auth"
    message). Stock ``mi_request`` raises ``Exception(f"Error {url}: {resp}")``
    on auth failure, so we match on the rendered message.
    """
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
    """Recursively mask secret values so request/response can be logged safely."""
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
        # Load userAgent and deviceId immediately if present
        if self.token:
            user_agent = self.token.get("userAgent")
            if user_agent:
                self.now_ua = user_agent

    async def _serviceLogin(self, uri, data=None):
        # Use a stable, app-like User-Agent derived from the deviceId so the Mi
        # passport sees a consistent "device" and stops re-challenging us (the
        # main cause of the login silently expiring). Keep it cached in the
        # token store so mi_request's self.now_ua stays in sync.
        device_id = self.token["deviceId"]
        user_agent = _account_user_agent(device_id)
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
        # Record (redacted) request context so a failed login can be diagnosed.
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
        # Ensure we load/preserve deviceId and userAgent before login
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

        # Always (re)derive the stable Android UA from the deviceId so an old
        # cached random browser UA can't keep triggering security verification.
        self.token["userAgent"] = _account_user_agent(self.token["deviceId"])

        device_id = self.token.get("deviceId")
        user_agent = self.token.get("userAgent")
        self.now_ua = user_agent
        
        # Save deviceId and userAgent to store immediately so they are not lost
        if self.token_store and device_id and user_agent:
            self.token_store.save_token({"deviceId": device_id, "userAgent": user_agent})

        # If we already have a valid token for this sid, return True immediately!
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
                
            # Only treat notification/captcha as failures when they carry a
            # *truthy* value: the Mi response ALWAYS includes ``notificationUrl``
            # and ``captchaUrl`` keys (value None when not required), so checking
            # ``"captchaUrl" in resp`` rejected even fully-successful logins.
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
            # Restore the deviceId and userAgent and save them back
            self.token = {"deviceId": device_id, "userAgent": user_agent}
            if self.token_store:
                self.token_store.save_token(self.token)
            
            # Extract a clean reason and log the *specific* failing request +
            # redacted response so the cause is diagnosable from the logs.
            err_msg = str(e)
            resp_dict = e.args[0] if (e.args and isinstance(e.args[0], dict)) else None
            if resp_dict is not None:
                if resp_dict.get("notificationUrl"):
                    err_msg = "安全验证未完成 (waiting for safety verification)"
                elif resp_dict.get("captchaUrl"):
                    err_msg = "需要输入图形验证码 (captcha required)"
                else:
                    err_msg = resp_dict.get("description") or resp_dict.get("desc") or "login failed"
                # Spell out exactly which success-check tripped.
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
                    "MiAccount login(%s) failed: %s | failing-check=[%s] | "
                    "request=%s | response=%s",
                    sid, err_msg, ", ".join(reasons) or "unknown",
                    getattr(self, "_last_request", None), _redact(resp_dict),
                )
            else:
                logger.error(
                    "MiAccount login(%s) errored: %s | request=%s",
                    sid, err_msg, getattr(self, "_last_request", None),
                )
            return False

    def _invalidate_sid(self, sid):
        """Drop the cached ``(ssecurity, serviceToken)`` for *sid* while keeping
        the long-lived passToken / deviceId so the next login refreshes silently.

        Mirrors Xiaoai-Claw-Addon's ``invalidateSid``. This is the missing piece
        that makes re-login actually work: an expired serviceToken must be wiped
        from the on-disk store, otherwise ``login`` keeps reloading and trusting
        it (via the early-return below) and the channel never recovers.
        """
        if not self.token:
            self.token = (self.token_store.load_token() if self.token_store else None) or {}
        removed = self.token.pop(sid, None) is not None
        if self.token_store:
            self.token_store.save_token(self.token)
        if removed:
            logger.info("invalidated cached serviceToken for sid=%s", sid)

    async def mi_request(self, sid, url, data, headers, relogin=True):
        # Stock mi_request's own relogin path sets self.token = None and calls
        # login(), but our login() then reloads the *just-rejected* serviceToken
        # from disk and short-circuits — so an expired token can never refresh.
        # Disable stock's relogin and drive it ourselves: on an auth error, wipe
        # the stale serviceToken (invalidate_sid) so login() really re-auths via
        # the passToken, then retry once. This is the fix for the login expiring.
        device_id = self.token.get("deviceId") if self.token else None
        user_agent = self.token.get("userAgent") if self.token else None
        had_sid_token = bool(self.token and sid in self.token)
        try:
            return await super().mi_request(sid, url, data, headers, relogin=False)
        except Exception as e:
            if relogin and had_sid_token and _is_auth_error(e):
                logger.info("serviceToken for sid=%s rejected (expired); re-logging in…", sid)
                self._invalidate_sid(sid)
                if await self.login(sid):
                    logger.info("re-login OK for sid=%s; retrying request", sid)
                    return await super().mi_request(sid, url, data, headers, relogin=False)
                logger.warning("re-login failed for sid=%s", sid)
            # If the token got cleared along the way, preserve deviceId/userAgent.
            if self.token is None and device_id and user_agent:
                self.token = {"deviceId": device_id, "userAgent": user_agent}
                if self.token_store:
                    self.token_store.save_token(self.token)
            raise



def _import_miservice():
    missing: list[str] = []
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        missing.append("aiohttp")
    MiAccount = MiNAService = MiTokenStore = None
    try:
        from miservice import MiAccount, MiNAService, MiTokenStore  # noqa: F401
    except ImportError:
        missing.append("miservice_fork")
    if missing:  # pragma: no cover - core deps, only if the env is broken
        raise XiaomiUnavailable(
            "小爱音箱接入缺少依赖：" + ", ".join(missing) + "。\n"
            "这些包已是 ssr-agent 的核心依赖；若仍缺失，请在当前环境重新安装：\n"
            "  pip install -e .   # 或： pip install miservice_fork aiohttp\n"
            "注意：是 'miservice_fork'（不是同名的 'miservice'），它提供 MiTokenStore。"
        )
    return CustomMiAccount, MiNAService, MiTokenStore


@dataclass
class SpeakerDevice:
    device_id: str
    name: str
    hardware: str
    did: str = ""


class XiaomiSpeaker:
    """Async wrapper around ``miservice`` for one speaker (login / ask / speak)."""

    def __init__(self, settings: Settings, cfg: XiaomiConfig):
        self.settings = settings
        self.cfg = cfg
        self._session = None
        self._account = None
        self._mina = None
        self.device: SpeakerDevice | None = None

    async def __aenter__(self) -> "XiaomiSpeaker":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        import aiohttp

        MiAccount, MiNAService, MiTokenStore = _import_miservice()
        tok = token_path(self.settings)
        logger.info(
            "connecting to Mi cloud: account=%s region=%s token_store=%s",
            self.cfg.account or "(empty!)", self.cfg.server_country, tok,
        )
        if not self.cfg.account or not self.cfg.password:
            raise RuntimeError("Mi account/password is empty — run: ssr channel config xiaomi")
        self._session = aiohttp.ClientSession()
        try:
            self._account = MiAccount(
                self._session,
                self.cfg.account,
                self.cfg.password,
                MiTokenStore(str(tok)),
            )
            self._mina = MiNAService(self._account)
            logger.info("Mi account/MiNAService initialised; logging in…")
            await self._login_or_diagnose()
            logger.info("Mi login OK; selecting speaker…")
            self.device = await self._select_device()
        except Exception:
            await self.close()
            raise
        logger.info(
            "selected speaker: name=%r deviceID=%s hardware=%s did=%s",
            self.device.name, self.device.device_id, self.device.hardware, self.device.did,
        )

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _login_or_diagnose(self) -> None:
        """Log in to the Mi passport, raising an *actionable* error on failure.

        ``MiAccount.login`` swallows errors and just returns False, so we run it
        and, on failure, replay the ``serviceLoginAuth2`` step ourselves to read
        the real response — most importantly any security-verification URL the
        account must visit before automated login will work.
        """
        ok = False
        try:
            ok = await self._account.login("micoapi")
        except Exception:
            logger.exception("MiAccount.login raised")
        if ok:
            return

        notif_url, reason = await _diagnose_login(self._account, self.cfg)
        if not notif_url:
            raise RuntimeError(reason + "\n\n" + _miot_cache_note())

        # Always persist the (very long) verification URL to a file so it can be
        # opened without terminal line-wrapping mangling it.
        url_file = self.settings.home / "xiaomi-verify-url.txt"
        try:
            url_file.write_text(notif_url + "\n", "utf-8")
        except OSError:
            pass

        # Safety verification needs an interactive browser + Enter. A headless
        # gateway (pm2 / pythonw / redirected stdin) can't do that — blocking on
        # input() would hang or raise EOFError. Fail fast with clear guidance so
        # the user completes verification once in a real terminal (which caches
        # the passToken), after which the gateway logs in from the cache.
        if not (sys.stdin is not None and sys.stdin.isatty()):
            raise XiaomiUnavailable(
                "小米账号需要【安全验证】，但当前是无终端环境（网关/后台），无法交互完成。\n"
                "请在【普通终端】里运行一次以完成验证并缓存登录态：\n"
                "    ssr channel login xiaomi\n"
                f"或手动打开此文件里的链接完成验证：{url_file}\n"
                "完成后重启网关即可（之后用缓存的 passToken 登录，不再需要验证）。"
            )

        # If it is a verification/captcha URL, print instructions and wait
        print("\n" + "=" * 80)
        print("小米账号需要【安全验证】才能登录（常见于服务器/新 IP 登录）。")
        print("请在浏览器中打开以下链接完成验证：")
        print("-" * 80)
        print(notif_url)
        print("-" * 80)
        print("【重要提示】")
        print("1. 该链接非常长，请务必完整复制。如果链接被终端换行，复制时可能会夹带")
        print("   多余的空格或换行符，请在浏览器地址栏手动删掉它们，否则会报 404！")
        print("2. 验证完成后，如果浏览器跳转到 401 页面（如 sts 接口 401 错误）是正常现象。")
        print("=" * 80 + "\n")

        import asyncio
        loop = asyncio.get_running_loop()
        # Wait for the user to complete verification and press enter. isatty()
        # can lie (it reports True under some wrappers that still have no real
        # stdin), so also treat EOFError as "no interactive input available" and
        # fail with the same actionable guidance instead of a bare traceback.
        try:
            await loop.run_in_executor(
                None, input,
                "【请在浏览器中完成验证，完成后在此处按回车键(Enter)以继续登录】...",
            )
        except EOFError:
            raise XiaomiUnavailable(
                "需要安全验证，但当前环境无法读取键盘输入（无交互终端）。\n"
                "请在【普通终端】里运行 `ssr channel login xiaomi` 完成验证，\n"
                f"或手动打开此文件里的链接完成验证：{url_file}\n"
                "完成后重启网关即可。"
            )

        # Mi may take a moment to register the just-completed verification, and a
        # single in-process retry often misses it. Retry a few times with a short
        # delay so a correctly-completed verification reliably lands.
        import asyncio as _asyncio
        for attempt in range(1, 4):
            print(f"\n正在重新尝试登录并检测验证状态…(第 {attempt}/3 次)")
            try:
                if await self._account.login("micoapi"):
                    print("[+] 验证成功，小米账号已成功登录并缓存登录态！")
                    return
            except Exception as e:
                logger.debug("尝试登录时出错: %s", e)
            if attempt < 3:
                await _asyncio.sleep(3)

        # Still failing after retries — surface actionable guidance.
        notif_url, reason = await _diagnose_login(self._account, self.cfg)
        raise RuntimeError(
            reason
            + "\n\n仍未通过验证。请确认你在浏览器里【真正完成】了验证（输入短信验证码或在"
            "“米家/小米账号”App 里点确认），而不仅仅是打开了链接。\n"
            "完成后再次运行： ssr channel login xiaomi\n\n"
            + _miot_cache_note()
        )


    async def _select_device(self) -> SpeakerDevice:
        try:
            devices = await self._mina.device_list() or []
        except Exception:
            logger.exception("device_list() failed (login or network problem)")
            raise
        logger.info("device_list returned %d device(s)", len(devices))
        for d in devices:
            logger.info(
                "  device: name=%r deviceID=%s hardware=%s miotDID=%s presence=%s",
                d.get("name"), d.get("deviceID"), d.get("hardware"),
                d.get("miotDID"), d.get("presence"),
            )
        if not devices:
            raise RuntimeError("未发现小爱设备，请确认账号下已绑定音箱。")

        def matches(d: dict) -> bool:
            if self.cfg.minaDeviceId:
                return d.get("deviceID") == self.cfg.minaDeviceId
            if self.cfg.speakerName:
                return self.cfg.speakerName in (d.get("name") or "")
            return False

        chosen = next((d for d in devices if matches(d)), devices[0])
        if not (self.cfg.minaDeviceId or self.cfg.speakerName):
            logger.warning(
                "no minaDeviceId/speakerName configured — defaulting to the first device %r. "
                "If that's the wrong speaker, set it via 'ssr channel config xiaomi'.",
                chosen.get("name"),
            )
        return SpeakerDevice(
            device_id=chosen.get("deviceID", ""),
            name=chosen.get("name", ""),
            hardware=chosen.get("hardware", self.cfg.hardware),
            did=str(chosen.get("miotDID", "")),
        )

    async def latest_ask(self) -> tuple[str, str] | None:
        """Return ``(request_id, question)`` for the most recent spoken query.

        Picks the message with the greatest ``timestamp_ms`` so the dedup id in
        the poll loop always tracks the newest utterance. Errors are logged (not
        swallowed) so a misconfigured/expired session is visible in the logs.
        """
        assert self.device is not None
        try:
            messages = await self._mina.get_latest_ask(self.device.device_id) or []
        except Exception:
            logger.exception("get_latest_ask() failed for device %s", self.device.device_id)
            return None

        logger.debug("get_latest_ask -> %d message(s): %s", len(messages), messages)
        best: tuple[int, str, str] | None = None  # (timestamp_ms, request_id, question)
        for msg in messages:
            ts = int(msg.get("timestamp_ms") or 0)
            rid = str(msg.get("request_id") or ts)
            answers = (msg.get("response") or {}).get("answer") or []
            for ans in answers:
                question = (ans.get("question") or "").strip()
                if question and (best is None or ts >= best[0]):
                    best = (ts, rid, question)
        if best is None:
            return None
        return (best[1], best[2])

    async def speak(self, text: str) -> None:
        assert self.device is not None
        if not text:
            return
        # XiaoAI TTS rejects very long strings; chunk on sentence boundaries.
        chunks = _chunk_for_tts(text)
        logger.info("speaking %d chunk(s) to %s", len(chunks), self.device.device_id)
        for i, chunk in enumerate(chunks):
            try:
                result = await self._mina.text_to_speech(self.device.device_id, chunk)
                logger.info("  TTS chunk %d/%d ok (%d chars), result=%s",
                            i + 1, len(chunks), len(chunk), result)
            except Exception:
                logger.exception("  TTS chunk %d/%d failed; aborting remaining chunks", i + 1, len(chunks))
                break


def find_miot_cache() -> dict | None:
    """Return the miot plugin's cached cloud OAuth info from ``~/.miot_cache``.

    The bundled ``miot`` plugin logs in with an **OAuth2** flow and caches the
    token under ``~/.miot_cache/cloud/`` (``oauth_info`` + ``uuid``). Returns the
    parsed dict (with a ``_path`` key) if present, else None.
    """
    base = Path("~/.miot_cache/cloud").expanduser()
    for name in ("oauth_info.dict", "oauth_info.json", "oauth_info"):
        p = base / name
        if not p.exists():
            continue
        try:
            raw = p.read_bytes()
            # storage may append a 32-byte sha256 integrity digest after the json
            for end in (len(raw), len(raw) - 32):
                if end <= 0:
                    continue
                try:
                    data = json.loads(raw[:end].decode("utf-8"))
                    if isinstance(data, dict):
                        data["_path"] = str(p)
                        return data
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        except OSError:
            continue
    return None


def _miot_cache_note() -> str:
    """A note about whether the miot OAuth cache can help (it can't, for ASR)."""
    info = find_miot_cache()
    if info is None:
        return (
            "提示：未找到 miot 插件的登录缓存（~/.miot_cache/cloud）。"
            "小爱对话频道使用的是小米账号 passport 登录（serviceToken），"
            "与 miot 插件的 OAuth 登录是两套不同的凭据。"
        )
    return (
        f"已检测到 miot 插件的 OAuth 登录缓存：{info.get('_path')} "
        f"(access_token={'有' if info.get('access_token') else '无'})。\n"
        "但它是【米家开放平台 OAuth2】凭据，只能用于设备控制，"
        "无法用于小爱音箱对话所需的 MiNA/passport 接口（ASR/get_latest_ask 需要 serviceToken）。\n"
        "因此请按上面的链接完成一次小米账号安全验证，让 passport 登录成功并缓存到 "
        "~/.ssr/xiaomi-token.json 后即可正常对话。"
    )


async def _diagnose_login(account, cfg: XiaomiConfig) -> tuple[str | None, str]:
    """Replay the login to surface why it failed (verification / bad password)."""
    import hashlib

    generic = (
        "小米登录失败。常见原因：账号或密码不正确、区域(region)不对、"
        "或账号触发了安全验证。请检查 ~/.ssr/xiaomi.json 后重试。"
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
                "user": cfg.account,
                "hash": hashlib.md5(cfg.password.encode()).hexdigest().upper(),
            }
            resp = await account._serviceLogin("serviceLoginAuth2", data)
    except Exception as e:
        logger.exception("login diagnosis failed")
        return None, f"{generic}\n(诊断时再次出错：{e})"

    code = resp.get("code")
    desc = resp.get("desc") or resp.get("description") or ""
    logger.error("login diagnosis: code=%s desc=%s keys=%s", code, desc, list(resp.keys()))

    notif = resp.get("notificationUrl")
    if notif:
        if notif.startswith("/"):
            notif = "https://account.xiaomi.com" + notif
        reason = (
            "小米账号需要【安全验证】才能登录（常见于服务器/新 IP 登录）。\n"
            "请在浏览器中打开以下链接完成验证（短信/设备确认）：\n\n"
            f"{notif}\n\n"
            "【注意】该链接非常长，请务必完整复制。如果链接换行，复制时可能夹带多余空格或换行符，请在浏览器地址栏中删除，否则会报 404！\n"
            "提示：验证完成后，浏览器如果跳转到 401 页面（如 sts 接口 401 错误）是正常现象。\n"
            "此时直接关闭浏览器，重新运行 ssr channel on xiaomi 即可正常登录。"
        )
        return notif, reason
    if resp.get("captchaUrl"):
        cap = resp["captchaUrl"]
        if cap.startswith("/"):
            cap = "https://account.xiaomi.com" + cap
        reason = (
            "小米登录需要图形验证码（captcha）。请先在浏览器登录一次小米账号完成验证：\n"
            f"    {cap}\n然后重试 ssr channel on xiaomi。"
        )
        return cap, reason
    if code == 70016 or "password" in desc.lower():
        reason = f"小米账号或密码不正确（code={code} {desc}）。请检查 ~/.ssr/xiaomi.json 的 account/password。"
        return None, reason
    if "userId" not in resp:
        reason = (
            f"小米登录未返回 userId（code={code} {desc}; keys={list(resp.keys())}）。"
            "通常是需要安全验证或账号/密码不正确。"
        )
        return None, reason
    return None, f"{generic}\n(code={code} {desc})"


def _chunk_for_tts(text: str, limit: int = 240) -> list[str]:
    text = " ".join(text.split())
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for token in text.replace("。", "。\n").replace(". ", ".\n").splitlines():
        if len(buf) + len(token) > limit and buf:
            chunks.append(buf.strip())
            buf = ""
        buf += token + " "
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text[:limit]]


def import_pass_token(settings: Settings, pass_token: str, user_id: str) -> None:
    """Seed the token store with a browser-obtained passToken + userId.

    This bypasses password login + safety verification entirely: a passToken is
    the account-level long-lived credential, so the next login exchanges it for a
    serviceToken without any ``securityStatus`` challenge. Get them from a logged
    in browser at i.mi.com / account.xiaomi.com (DevTools → Cookies → passToken,
    userId). The existing deviceId/userAgent are preserved.
    """
    tp = token_path(settings)
    try:
        tok = json.loads(tp.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        tok = {}
    if not tok.get("deviceId"):
        try:
            from miservice.miaccount import get_random
            tok["deviceId"] = get_random(16).upper()
        except Exception:
            tok["deviceId"] = "0123456789ABCDEF"
    tok["userAgent"] = _account_user_agent(tok["deviceId"])
    tok["passToken"] = pass_token.strip()
    tok["userId"] = str(user_id).strip()
    # Drop any stale per-sid serviceToken so login re-derives it from passToken.
    tok.pop("micoapi", None)
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps(tok, ensure_ascii=False, indent=2), "utf-8")
    try:
        tp.chmod(0o600)
    except OSError:
        pass


def interactive_login(
    settings: Settings, pass_token: str | None = None, user_id: str | None = None
) -> str:
    """Run the Mi login interactively (TTY) so safety verification can complete.

    Returns "OK" once a passToken is cached, otherwise an error string. Intended
    for ``ssr channel login xiaomi`` — run it once in a real terminal; the cached
    token then lets the headless gateway log in without re-verification.

    If ``pass_token``/``user_id`` are given, they are imported first (bypassing
    password login + safety verification) and then validated by logging in.
    """
    import asyncio

    if pass_token:
        if not user_id:
            return "提供 --pass-token 时必须同时提供 --user-id（两者都在浏览器 Cookie 里）。"
        import_pass_token(settings, pass_token, user_id)

    cfg = load_config(settings)
    if cfg is None or not cfg.account:
        return "小爱音箱未配置，请先运行: ssr channel config xiaomi"
    if not cfg.password and not pass_token:
        return "未配置密码。请运行 ssr channel config xiaomi，或用 --pass-token 登录。"

    speaker = XiaomiSpeaker(settings, cfg)

    async def _run() -> None:
        try:
            await speaker.connect()
        finally:
            await speaker.close()

    err: str | None = None
    try:
        asyncio.run(_run())
    except Exception as e:  # device selection may fail even if login succeeded
        err = str(e)

    # Success is defined by a cached passToken (login completed), regardless of
    # whether the later device-selection step succeeded.
    try:
        tok = json.loads(token_path(settings).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        tok = {}
    if tok.get("passToken"):
        return "OK"
    return "FAIL: " + (err or "登录未完成（未获得 passToken）")


# --------------------------------------------------- headed browser CDP login
def _find_browser() -> str | None:
    """Locate a Chrome/Edge/Chromium executable to drive via DevTools Protocol."""
    import os
    import shutil

    for n in ("chrome", "google-chrome", "google-chrome-stable", "chromium",
              "chromium-browser", "msedge"):
        p = shutil.which(n)
        if p:
            return p
    candidates: list[Path] = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
                candidates.append(Path(base) / "Microsoft/Edge/Application/msedge.exe")
    elif sys.platform == "darwin":
        candidates += [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    for c in candidates:
        try:
            if c.exists():
                return str(c)
        except OSError:
            pass
    return None


def _cdp_get_all_cookies(port: int) -> list[dict]:
    """All browser cookies via CDP ``Storage.getCookies`` (includes httpOnly)."""
    import asyncio

    import httpx

    try:
        info = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3.0).json()
    except Exception:
        return []
    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        return []

    async def _go() -> list[dict]:
        import websockets

        async with websockets.connect(ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            for _ in range(50):
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    return msg.get("result", {}).get("cookies", []) or []
            return []

    try:
        return asyncio.run(_go())
    except Exception:
        return []


def _pick_cookie(cookies: list[dict], name: str) -> str | None:
    """Value of cookie ``name`` (preferring a mi.com-scoped one), or None."""
    matches = [c for c in cookies if c.get("name") == name and c.get("value")]
    if not matches:
        return None
    matches.sort(key=lambda c: "mi.com" not in (c.get("domain") or ""))
    return matches[0]["value"]


def _attempt_login(settings: Settings) -> bool:
    """Try a full login with whatever is cached; True if a serviceToken results."""
    import asyncio

    cfg = load_config(settings)
    if cfg is None or not cfg.account:
        return False
    speaker = XiaomiSpeaker(settings, cfg)

    async def _run() -> None:
        try:
            await speaker.connect()
        finally:
            await speaker.close()

    try:
        asyncio.run(_run())
    except Exception:
        pass
    try:
        tok = json.loads(token_path(settings).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        tok = {}
    return "micoapi" in tok  # serviceToken obtained → real success


def browser_login(settings: Settings, timeout: float = 300.0, port: int = 9222) -> str:
    """Open a headed Chrome at the Mi login page, wait for the user to sign in,
    then harvest passToken+userId from the browser via the DevTools Protocol.

    A real browser can complete the safety verification (SMS / app confirm) that
    the headless password flow cannot; we read the resulting (httpOnly) passToken
    cookie via CDP ``Storage.getCookies`` and cache it. Returns "OK" on success.
    """
    import time as _time

    browser = _find_browser()
    if not browser:
        return ("未找到 Chrome/Edge/Chromium。请安装其一，或改用 "
                "`ssr channel login xiaomi --pass-token <...> --user-id <...>`。")

    profile = settings.home / "xiaomi-chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    login_url = "https://account.xiaomi.com/pass/serviceLogin?sid=micoapi&_locale=zh_CN"
    args = [
        browser,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        login_url,
    ]
    print("正在打开浏览器，请在其中登录小米账号（可完成短信/设备安全验证）。")
    print("登录成功后会自动从浏览器读取 token，无需手动复制。")
    try:
        proc = subprocess.Popen(args)
    except OSError as e:
        return f"无法启动浏览器：{e}"

    try:
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if proc.poll() is not None:
                return "浏览器已被关闭，未获取到 token。"
            cookies = _cdp_get_all_cookies(port)
            pass_token = _pick_cookie(cookies, "passToken")
            user_id = _pick_cookie(cookies, "userId")
            if pass_token and user_id:
                import_pass_token(settings, pass_token, user_id)
                print("[+] 已从浏览器获取 passToken，正在验证登录…")
                ok = _attempt_login(settings)
                return "OK" if ok else (
                    "已获取并缓存 passToken；登录态已写入 "
                    f"{token_path(settings)}。可启动网关验证设备。"
                )
            _time.sleep(2)
        return "TIMEOUT: 超时未检测到登录（未读到 passToken/userId）。"
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
