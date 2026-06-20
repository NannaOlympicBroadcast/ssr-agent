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
    if missing:  # pragma: no cover - optional dependency
        raise XiaomiUnavailable(
            "小爱音箱接入缺少依赖：" + ", ".join(missing) + "。请安装其一：\n"
            '  pip install "ssr-agent[xiaomi]"\n'
            "  # 或直接安装： pip install miservice_fork aiohttp\n"
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
        logger.info("Mi account/MiNAService initialised; selecting speaker…")
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
