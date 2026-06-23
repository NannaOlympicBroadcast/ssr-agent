"""XiaoAI speaker (小爱音箱) channel.

Adds the speaker as an SSR input channel: spoken queries are polled from the Mi
cloud (ASR) and answered by the agent, with the reply spoken back via the
speaker's text-to-speech. Credentials are shared with the bundled ``miot``
plugin through ``~/.ssr/xiaomi.json``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from ssr.channels.base import AbstractChannel
from ssr.config import Settings

logger = logging.getLogger("ssr.xiaomi")


def _setup_logging() -> None:
    """Send ssr.xiaomi logs to stdout. Set SSR_XIAOMI_DEBUG=1 for DEBUG detail."""
    debug = os.environ.get("SSR_XIAOMI_DEBUG", "").lower() in ("1", "true", "yes")
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger("ssr.xiaomi")
    root.setLevel(level)
    if not any(getattr(h, "_ssr_xiaomi", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(asctime)s] [xiaomi] %(levelname)s %(message)s", "%H:%M:%S"))
        handler._ssr_xiaomi = True  # type: ignore[attr-defined]
        root.addHandler(handler)


class XiaomiChannel(AbstractChannel):
    name = "xiaomi"

    def __init__(self):
        self._speaker = None
        self._loop = None

    # ------------------------------------------------------------- configure
    def configure(self, settings: Settings) -> None:
        from ssr.integrations.xiaomi import configure_interactive

        configure_interactive(settings)

    # ----------------------------------------------------------------- serve
    def serve(self, settings: Settings) -> None:
        from ssr.agent.core import SSRAgent
        from ssr.approval import IMApprovalHandler
        from ssr.channels.slash_im import handle_slash_im
        from ssr.integrations.xiaomi import (
            XiaomiSpeaker,
            XiaomiUnavailable,
            load_config,
        )

        _setup_logging()
        cfg = load_config(settings)
        if cfg is None or not cfg.account or not cfg.password:
            raise SystemExit("小爱音箱未配置。请先运行: ssr channel config xiaomi")

        logger.info(
            "starting XiaoAI channel: account=%s region=%s device=%s name=%r wake_word=%r",
            cfg.account, cfg.server_country, cfg.minaDeviceId or "(auto)",
            cfg.speakerName or "(auto)", cfg.wake_word or "(none)",
        )

        # default_cwd is a starting directory only, not a hard restriction.
        if cfg.default_cwd:
            settings.project_dir = Path(cfg.default_cwd).expanduser()

        agent = SSRAgent(settings)

        try:
            asyncio.run(self._serve_async(settings, cfg, agent, handle_slash_im, IMApprovalHandler, XiaomiSpeaker, XiaomiUnavailable))
        except SystemExit:
            raise
        except KeyboardInterrupt:
            logger.info("XiaoAI channel stopped by user.")
        except Exception:
            logger.exception("XiaoAI channel crashed")
            raise

    async def _serve_async(
        self, settings, cfg, agent, handle_slash_im, IMApprovalHandler, XiaomiSpeaker, XiaomiUnavailable
    ) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            speaker = XiaomiSpeaker(settings, cfg)
            await speaker.connect()
        except XiaomiUnavailable as e:
            logger.error("%s", e)
            raise SystemExit(str(e))
        except Exception as e:
            logger.exception("failed to connect to the Mi cloud / select speaker")
            raise SystemExit(f"小爱音箱连接失败：{e}")
        self._speaker = speaker
        target = speaker.device.device_id

        # Speak replies back through the speaker.
        def speak_sync(_target: str, text: str) -> None:
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(speaker.speak(text), self._loop)

        # A speaker has no usable approval UX — the poll loop is busy running the
        # turn, so a spoken "/approve" can't be read until the turn ends and the
        # request would just time out (deny). So auto-approve every command by
        # default (cfg.auto_approve). Set "auto_approve": false in xiaomi.json to
        # restore voice/out-of-band /approve prompts via TTS instead.
        if getattr(cfg, "auto_approve", True):
            from ssr.approval import AutoApprovalHandler

            agent.toolkit.approval_handler = AutoApprovalHandler()
            logger.info("auto-approve enabled: all commands run without confirmation")
        else:
            agent.toolkit.approval_handler = IMApprovalHandler(send_fn=speak_sync)
        agent.active_im_context = ("xiaomi", target)

        print(
            f"SSR XiaoAI channel serving on '{speaker.device.name}' "
            f"(deviceID={target}); waiting for speech…"
        )
        await speaker.speak("SSR 已接入，请对我说话。")

        last_request_id: str | None = None
        first_poll = True
        poll_interval = 1.0
        poll_count = 0
        logger.info("entering poll loop (interval=%.1fs). Speak to the speaker now.", poll_interval)
        while True:
            poll_count += 1
            try:
                ask = await speaker.latest_ask()
            except Exception:
                logger.exception("poll #%d failed; continuing", poll_count)
                await asyncio.sleep(poll_interval)
                continue

            if ask is None:
                logger.debug("poll #%d: no recognised utterance", poll_count)
            else:
                request_id, question = ask
                if request_id == last_request_id:
                    logger.debug("poll #%d: same utterance id=%s (already handled)", poll_count, request_id)
                else:
                    logger.info("poll #%d: NEW utterance id=%s question=%r", poll_count, request_id, question)
                    last_request_id = request_id
                    # On the very first poll, only set the baseline so we don't
                    # answer a stale pre-existing utterance.
                    if first_poll:
                        logger.info("  -> baseline only (first poll); not answering this stale utterance")
                    else:
                        await self._handle_utterance(
                            agent, question, target, speaker, handle_slash_im, settings
                        )
            first_poll = False
            await asyncio.sleep(poll_interval)

    async def _handle_utterance(
        self, agent, question: str, target: str, speaker, handle_slash_im, settings
    ) -> None:
        cfg = speaker.cfg
        # Optional wake word: only act on queries that contain it (then strip it).
        if cfg.wake_word:
            if cfg.wake_word not in question:
                logger.info("  -> ignored: wake word %r not in utterance", cfg.wake_word)
                return
            question = question.replace(cfg.wake_word, "", 1).strip()
            if not question:
                logger.info("  -> wake word only, nothing to do")
                return

        # Slash commands stay in sync with CLI / other channels.
        if question.startswith("/"):
            logger.info("  -> handling as slash command: %s", question)
            def reply_fn(_t, text):
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(speaker.speak(text), self._loop)
            try:
                handle_slash_im(question, agent, settings, target, reply_fn)
            except Exception as e:
                logger.exception("slash command failed")
                await speaker.speak(f"命令出错：{e}")
            return

        # Run the (blocking) agent turn off the event loop so polling/TTS stay live.
        logger.info("  -> running agent for utterance…")
        loop = asyncio.get_running_loop()

        def run_turn() -> str:
            agent.active_im_context = ("xiaomi", target)
            try:
                return agent.run(question)
            except Exception as e:
                logger.exception("agent.run failed")
                return f"出错了：{e}"

        reply = await loop.run_in_executor(None, run_turn)
        logger.info("  -> agent reply (%d chars): %s", len(reply or ""), (reply or "")[:200])
        await speaker.speak(reply)

    # ------------------------------------------------------------ outbound
    def send_message(self, target: str, text: str) -> None:
        if self._speaker is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._speaker.speak(text), self._loop)

    def send_tts(self, settings: Settings, text: str) -> str:
        """Speak ``text`` on the speaker — used as a push channel.

        Reuses the live speaker when the channel is serving; otherwise does a
        one-off login → speak → close so a notification can be pushed by voice
        even when the XiaoAI channel isn't running.
        """
        text = (text or "").strip()
        if not text:
            return "ERROR: empty message"
        if self._speaker is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._speaker.speak(text), self._loop)
            return "Spoken on the live XiaoAI speaker."

        from ssr.integrations.xiaomi import XiaomiSpeaker, load_config

        cfg = load_config(settings)
        if cfg is None or not cfg.account:
            return "ERROR: XiaoAI not configured — run: ssr channel config xiaomi"

        async def _run() -> None:
            speaker = XiaomiSpeaker(settings, cfg)
            try:
                await speaker.connect()
                await speaker.speak(text)
            finally:
                await speaker.close()

        try:
            asyncio.run(_run())
            return "Spoken on the XiaoAI speaker (one-off)."
        except Exception as e:
            return f"ERROR speaking on XiaoAI: {e}"

    def send_file(self, target: str, path: str, mime_type: str) -> None:
        # A speaker has no display; announce the file by voice instead.
        name = Path(path).name
        self.send_message(target, f"已生成文件 {name}，请到关联的设备查看。")

    def send_image(self, target: str, path_or_bytes) -> None:
        self.send_message(target, "已生成一张图片，请到关联的设备查看。")
