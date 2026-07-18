"""Core SDK: connect to a XiaoAI speaker with a token, listen for ASR, speak TTS.

Two things this class does, mirroring the mechanism used by ssr-agent's XiaoAI
channel (itself modeled on https://github.com/ZhengXieGang/Xiaoai-Claw-Addon):

* **Input (ASR)** — poll the speaker's conversation history (falling back to
  the ubus ``nlp_result_get``) for the user's latest spoken query.
* **Output (TTS takeover)** — pause whatever the speaker is playing and speak
  arbitrary text back through it, via MiIO ``play-text`` (falls back to MiNA
  ``text_to_speech`` where the former is unsupported).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from .account import CustomMiAccount, diagnose_login
from .exceptions import LoginRequired, SafetyVerificationRequired, XiaomiUnavailable
from .token_store import default_token_path, load_token

logger = logging.getLogger("xiaomi_speaker_sdk")


def _import_miservice():
    missing: list[str] = []
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        missing.append("aiohttp")
    MiNAService = MiTokenStore = MiIOService = None
    try:
        from miservice import MiIOService, MiNAService, MiTokenStore  # noqa: F401
    except ImportError:
        missing.append("miservice_fork")
    if missing:
        raise XiaomiUnavailable(
            "Missing dependencies: " + ", ".join(missing) + ". Install with:\n"
            "  pip install xiaomi-speaker-sdk\n"
            "(needs 'miservice_fork', not the same-named 'miservice')."
        )
    return MiNAService, MiTokenStore, MiIOService


# Per-hardware MiIO play-text action (siid, aiid). Used when MiNA's
# text_to_speech silently no-ops on a speaker (acks code 0 but never speaks).
# Values mirror the community xiaogpt hardware map.
_TTS_MIIO_COMMANDS = {
    "LX04": (5, 1), "LX06": (5, 1), "LX01": (5, 1), "LX5A": (5, 1), "LX05A": (5, 1),
    "L05B": (5, 3), "L05C": (5, 3), "S12A": (5, 1), "S12": (5, 1), "L06A": (5, 1),
    "L07A": (5, 1), "L09A": (3, 1), "L15A": (7, 3), "L17A": (7, 3), "X08E": (7, 3),
    "X10A": (7, 3), "X6A": (7, 3), "X08C": (7, 3),
}


@dataclass
class SpeakerDevice:
    """One speaker on the Mi account, as returned by ``MiNAService.device_list()``."""

    device_id: str
    name: str
    hardware: str
    did: str = ""  # MIoT DID, if this speaker is also bound in Mi Home


@dataclass
class XiaomiSpeakerConfig:
    account: str = ""             # Mi account (id / email / phone); optional if a token is cached
    password: str = ""            # optional if a passToken is already cached
    server_country: str = "cn"    # cn / de / i2 / ru / sg / us
    mina_device_id: str = ""      # preferred selector: exact MiNA deviceID
    speaker_name: str = ""        # fallback selector: substring of the device name
    hardware: str = ""            # e.g. "L05C" — used for the TTS command lookup if unset elsewhere
    tts_command: str = ""         # override MiIO play-text action as "siid-aiid"


class XiaomiSpeaker:
    """Async client for one XiaoAI speaker: connect once, then ``listen_asr`` / ``speak``.

    Example::

        from xiaomi_speaker_sdk import XiaomiSpeaker, XiaomiSpeakerConfig

        cfg = XiaomiSpeakerConfig(speaker_name="客厅")
        speaker = XiaomiSpeaker(cfg, token_path="~/.xiaomi_speaker_sdk/token.json")

        async def main():
            await speaker.connect()
            await speaker.speak("SDK 已接管，请说话。")
            async for request_id, question in speaker.listen_asr():
                print("heard:", question)
                await speaker.speak(f"你说了：{question}")

    A token must already be cached at ``token_path`` (see
    :mod:`xiaomi_speaker_sdk.browser_auth` or
    :func:`xiaomi_speaker_sdk.token_store.import_pass_token`), or
    ``config.account``/``config.password`` must be set for a first-time
    password login (which may require completing safety verification —
    prefer the browser flow to avoid that).
    """

    def __init__(
        self,
        config: XiaomiSpeakerConfig | None = None,
        token_path: str | Path | None = None,
    ):
        self.config = config or XiaomiSpeakerConfig()
        self.token_path = Path(token_path).expanduser() if token_path else default_token_path()
        self._session = None
        self._account = None
        self._mina = None
        self._miio = None
        self.device: SpeakerDevice | None = None

    async def __aenter__(self) -> "XiaomiSpeaker":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------- connect
    async def connect(self) -> None:
        import aiohttp

        MiNAService, MiTokenStore, MiIOService = _import_miservice()

        cached = load_token(self.token_path)
        has_token = bool(cached.get("passToken"))
        if not has_token and not (self.config.account and self.config.password):
            raise LoginRequired(
                f"No cached token at {self.token_path} and no account/password "
                "configured. Run xiaomi_speaker_sdk.browser_auth.extract_token() "
                "once, or set config.account/config.password."
            )

        self._session = aiohttp.ClientSession()
        try:
            self._account = CustomMiAccount(
                self._session,
                self.config.account or (cached.get("userId") or ""),
                self.config.password or "",
                MiTokenStore(str(self.token_path)),
            )
            self._mina = MiNAService(self._account)
            self._miio = MiIOService(self._account)
            await self._login_or_raise()
            self.device = await self._select_device()
        except Exception:
            await self.close()
            raise
        logger.info(
            "connected: speaker=%r deviceID=%s hardware=%s did=%s",
            self.device.name, self.device.device_id, self.device.hardware, self.device.did,
        )

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _login_or_raise(self) -> None:
        try:
            ok = await self._account.login("micoapi")
        except Exception:
            logger.exception("MiAccount.login raised")
            ok = False
        if ok:
            return
        notif_url, reason = await diagnose_login(
            self._account, self.config.account, self.config.password
        )
        if notif_url:
            raise SafetyVerificationRequired(reason, verification_url=notif_url)
        raise LoginRequired(reason)

    async def _select_device(self) -> SpeakerDevice:
        devices = await self._mina.device_list() or []
        if not devices:
            raise LoginRequired("No XiaoAI speakers found on this Mi account.")

        def matches(d: dict) -> bool:
            if self.config.mina_device_id:
                return d.get("deviceID") == self.config.mina_device_id
            if self.config.speaker_name:
                return self.config.speaker_name in (d.get("name") or "")
            return False

        chosen = next((d for d in devices if matches(d)), devices[0])
        return SpeakerDevice(
            device_id=chosen.get("deviceID", ""),
            name=chosen.get("name", ""),
            hardware=chosen.get("hardware", self.config.hardware),
            did=str(chosen.get("miotDID", "")),
        )

    async def list_devices(self) -> list[SpeakerDevice]:
        """All XiaoAI speakers on this Mi account (does not change the selected device)."""
        devices = await self._mina.device_list() or []
        return [
            SpeakerDevice(
                device_id=d.get("deviceID", ""),
                name=d.get("name", ""),
                hardware=d.get("hardware", ""),
                did=str(d.get("miotDID", "")),
            )
            for d in devices
        ]

    # ------------------------------------------------------------ ASR (in)
    async def latest_ask(self) -> tuple[str, str] | None:
        """``(request_id, question)`` for the most recent spoken query, or ``None``."""
        assert self.device is not None
        ask = await self._conversation_ask()
        if ask is not None:
            return ask
        return await self._nlp_result_ask()

    async def _conversation_ask(self, _retried: bool = False) -> tuple[str, str] | None:
        import time as _t

        acc = self._account
        if not (acc.token and "micoapi" in acc.token):
            try:
                if not await acc.login("micoapi"):
                    return None
            except Exception:
                return None

        try:
            service_token = acc.token["micoapi"][1]
            user_id = str(acc.token["userId"])
        except (KeyError, IndexError, TypeError):
            return None

        hardware = self.device.hardware or self.config.hardware or ""
        ts = int(_t.time() * 1000)
        url = (
            "https://userprofile.mina.mi.com/device_profile/v2/conversation"
            f"?source=dialogu&hardware={hardware}&timestamp={ts}&limit=2"
        )
        cookies = {
            "userId": user_id,
            "serviceToken": service_token,
            "deviceId": self.device.device_id,
        }
        headers = {"User-Agent": getattr(acc, "now_ua", "")}
        try:
            async with acc.session.get(url, cookies=cookies, headers=headers) as r:
                resp = await r.json(content_type=None)
        except Exception as e:
            logger.debug("conversation API request failed (%s); ubus fallback", e)
            return None

        code = resp.get("code") if isinstance(resp, dict) else None
        if code != 0:
            msg = (resp or {}).get("message", "")
            if not _retried and ("auth" in str(msg).lower() or code in (401, 2)):
                try:
                    acc._invalidate_sid("micoapi")
                    if await acc.login("micoapi"):
                        return await self._conversation_ask(_retried=True)
                except Exception:
                    pass
            return None

        raw = resp.get("data")
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            return None
        records = (data or {}).get("records") or []
        best: tuple[int, str, str] | None = None
        for rec in records:
            q = (rec.get("query") or "").strip()
            t = int(rec.get("time") or 0)
            rid = str(rec.get("requestId") or rec.get("time") or t)
            if q and (best is None or t >= best[0]):
                best = (t, rid, q)
        if best is None:
            return None
        return (best[1], best[2])

    async def _nlp_result_ask(self) -> tuple[str, str] | None:
        try:
            raw = await self._mina.ubus_request(
                self.device.device_id, "nlp_result_get", "mibrain", {}
            )
        except Exception:
            return None

        data = (raw or {}).get("data") or {}
        if data.get("code") != 0:
            return None
        try:
            result = (json.loads(data.get("info") or "{}") or {}).get("result") or []
        except (json.JSONDecodeError, TypeError):
            return None

        best: tuple[int, str, str] | None = None
        for item in result:
            if "nlp" not in item:
                continue
            try:
                nlp = json.loads(item["nlp"])
                ts = int(nlp["meta"]["timestamp"])
                rid = str(nlp["meta"]["request_id"])
                for ans in nlp.get("response", {}).get("answer", []) or []:
                    q = ((ans.get("intention") or {}).get("query") or "").strip()
                    if q and (best is None or ts >= best[0]):
                        best = (ts, rid, q)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        if best is None:
            return None
        return (best[1], best[2])

    async def listen_asr(
        self,
        poll_interval: float = 1.0,
        skip_backlog: bool = True,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(request_id, question)`` for each *new* spoken utterance, forever.

        With ``skip_backlog=True`` (default) the first poll only records a
        baseline and doesn't yield, so a stale pre-existing utterance isn't
        replayed when you start listening.
        """
        last_request_id: str | None = None
        first_poll = True
        while True:
            ask = await self.latest_ask()
            if ask is not None:
                request_id, question = ask
                if request_id != last_request_id:
                    last_request_id = request_id
                    if first_poll and skip_backlog:
                        pass
                    else:
                        yield request_id, question
            first_poll = False
            await asyncio.sleep(poll_interval)

    async def listen(
        self,
        callback: Callable[[str, str], Awaitable[None]],
        poll_interval: float = 1.0,
        skip_backlog: bool = True,
    ) -> None:
        """Convenience runner: call ``await callback(request_id, question)`` per utterance."""
        async for request_id, question in self.listen_asr(poll_interval, skip_backlog):
            await callback(request_id, question)

    # --------------------------------------------------------- TTS (out)
    def _tts_command(self) -> tuple[int, int] | None:
        if self.config.tts_command:
            try:
                siid, aiid = self.config.tts_command.split("-")
                return (int(siid), int(aiid))
            except (ValueError, AttributeError):
                pass
        hw = (self.device.hardware if self.device else "") or self.config.hardware or ""
        return _TTS_MIIO_COMMANDS.get(hw.upper())

    async def _speak_chunk(self, text: str) -> bool:
        cmd = self._tts_command()
        did = self.device.did if self.device else ""
        if cmd and did and self._miio is not None:
            try:
                code = await self._miio.miot_action(did, list(cmd), [text])
                if code == 0:
                    return True
            except Exception:
                pass
        try:
            await self._mina.text_to_speech(self.device.device_id, text)
            return True
        except Exception:
            logger.exception("MiNA TTS failed")
            return False

    async def _pause_playback(self) -> None:
        """Pause any current media so a takeover TTS reply is actually heard."""
        try:
            await self._mina.player_pause(self.device.device_id)
        except Exception:
            pass

    async def speak(self, text: str) -> None:
        """Take over the speaker and speak ``text`` (pauses playback first)."""
        assert self.device is not None
        text = strip_markdown_for_tts(text)
        if not text:
            return
        chunks = chunk_for_tts(text)
        await self._pause_playback()
        await asyncio.sleep(0.4)
        for chunk in chunks:
            if not await self._speak_chunk(chunk):
                logger.warning("TTS chunk failed; aborting remaining chunks")
                break


# --------------------------------------------------------------- text prep
_MD_FENCE_RE = re.compile(r"```[^\n]*\n?|~~~[^\n]*\n?")
_MD_HR_RE = re.compile(r"(?m)^\s*([-*_])(?:\s*\1){2,}\s*$")
_MD_IMG_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_REF_LINK_RE = re.compile(r"!?\[([^\]]*)\]\[[^\]]*\]")
_MD_AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^>]+)>")
_MD_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_MD_BLOCKQUOTE_RE = re.compile(r"(?m)^\s{0,3}>\s?")
_MD_LIST_RE = re.compile(r"(?m)^\s*([-*+])\s+")
_MD_TABLE_DELIM_RE = re.compile(r"(?m)^[\s:|]*-[\s:|-]*$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_EMPHASIS_RE = re.compile(r"\*{1,3}|_{1,3}|~{1,2}|`+")


def strip_markdown_for_tts(text: str) -> str:
    """Flatten Markdown to plain prose so the speaker reads words, not symbols."""
    if not text:
        return ""
    t = _MD_FENCE_RE.sub("", text)
    t = _MD_HR_RE.sub("", t)
    t = _MD_AUTOLINK_RE.sub(r"\1", t)
    t = _MD_IMG_LINK_RE.sub(r"\1", t)
    t = _MD_REF_LINK_RE.sub(r"\1", t)
    t = _MD_HEADING_RE.sub("", t)
    t = _MD_BLOCKQUOTE_RE.sub("", t)
    t = _MD_LIST_RE.sub("", t)
    t = _MD_TABLE_DELIM_RE.sub("", t)
    t = _HTML_TAG_RE.sub("", t)
    t = _MD_EMPHASIS_RE.sub("", t)
    t = t.replace("|", " ")
    return t.strip()


def chunk_for_tts(text: str, limit: int = 240) -> list[str]:
    """XiaoAI TTS rejects very long strings; chunk on sentence boundaries."""
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
