"""Wechat channel adapter using the optional weixin-bot-sdk package.

Grounded in epiral/weixin-bot: the Python SDK exposes WeixinBot, login(),
on_message, reply(), send_typing(), and run().
"""
from __future__ import annotations

from ..config import Settings


def configure_interactive(settings: Settings) -> None:
    settings.ensure_dirs()
    print("Wechat channel uses weixin-bot-sdk zero-config QR login; no secrets stored.")
    print("Run: ssr channel on wechat")


def serve(settings: Settings) -> None:
    try:
        from weixin_bot import WeixinBot  # type: ignore
    except Exception as e:
        raise SystemExit("Wechat SDK missing. Install with: pip install weixin-bot-sdk") from e
    from ..agent.core import SSRAgent
    from ..slash import handle_text_command

    bot = WeixinBot()
    bot.login()
    agent = SSRAgent(settings)

    @bot.on_message
    async def _handle(msg):
        text = getattr(msg, "text", "") or ""
        await bot.send_typing(msg.user_id)
        if text.startswith("/"):
            reply = handle_text_command(text, agent, settings) or "OK"
        else:
            reply = agent.run(text)
        await bot.reply(msg, reply)

    bot.run()
