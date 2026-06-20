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
    return MiAccount, MiNAService, MiTokenStore


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
        reason = await _diagnose_login(self._account, self.cfg)
        raise RuntimeError(reason + "\n\n" + _miot_cache_note())

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


async def _diagnose_login(account, cfg: XiaomiConfig) -> str:
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
        account.token = {"deviceId": (get_random(16).upper() if get_random else "0123456789ABCDEF")}
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
        return f"{generic}\n(诊断时再次出错：{e})"

    code = resp.get("code")
    desc = resp.get("desc") or resp.get("description") or ""
    logger.error("login diagnosis: code=%s desc=%s keys=%s", code, desc, list(resp.keys()))

    notif = resp.get("notificationUrl")
    if notif:
        if notif.startswith("/"):
            notif = "https://account.xiaomi.com" + notif
        return (
            "小米账号需要【安全验证】才能登录（常见于服务器/新 IP 登录）。\n"
            "请在浏览器打开下面的链接，用该小米账号完成验证（短信/设备确认）：\n"
            f"    {notif}\n"
            "完成后重新运行：ssr channel on xiaomi（验证一次后会缓存登录态）。"
        )
    if resp.get("captchaUrl"):
        cap = resp["captchaUrl"]
        if cap.startswith("/"):
            cap = "https://account.xiaomi.com" + cap
        return (
            "小米登录需要图形验证码（captcha）。请先在浏览器登录一次小米账号完成验证：\n"
            f"    {cap}\n然后重试 ssr channel on xiaomi。"
        )
    if code == 70016 or "password" in desc.lower():
        return f"小米账号或密码不正确（code={code} {desc}）。请检查 ~/.ssr/xiaomi.json 的 account/password。"
    if "userId" not in resp:
        return (
            f"小米登录未返回 userId（code={code} {desc}; keys={list(resp.keys())}）。"
            "通常是需要安全验证或账号/密码不正确。"
        )
    return f"{generic}\n(code={code} {desc})"


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
