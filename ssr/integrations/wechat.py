"""WeChat Bot integration using weixin-bot-sdk."""

from __future__ import annotations

import threading

from ..config import Settings

def serve_long_connection(settings: Settings) -> None:
    """Run the WeChat bot over a long-poll loop."""
    try:
        from weixin_bot import WeixinBot
    except ImportError:
        raise SystemExit("WeChat SDK not installed. Please run: pip install weixin-bot-sdk")

    from ..agent.core import SSRAgent
    from ..slash import handle as handle_slash
    from rich.console import Console

    console = Console()

    # Store settings in a global accessible to the tool
    import sys
    sys.modules['ssr.integrations.wechat'].active_bot = None

    # We use a dummy console for the agent
    agent = SSRAgent(settings)

    bot = WeixinBot(token_path=str(settings.home / "wechat_credentials.json"))
    sys.modules['ssr.integrations.wechat'].active_bot = bot

    # Setup fetch resource lambda
    def fetch_resource(msg, file_key, rtype):
        if rtype == "image":
            # Assuming weixin_bot handles image raw fetching
            return msg.raw.get("image_data")
        return None

    @bot.on_message
    async def handle_msg(msg):
        text = msg.text

        if text.startswith("/"):
            # Mock console output for slash commands over IM
            class MockConsole:
                def print(self, *args, **kwargs):
                    pass
                def status(self, *args, **kwargs):
                    class CM:
                        def __enter__(self): pass
                        def __exit__(self, *args): pass
                    return CM()

            handle_slash(text, agent, settings, MockConsole())
            await bot.reply(msg, f"Executed command: {text}")
            return

        parts = []
        if msg.type == "image":
            parts.append({"type": "text", "text": "[image received]"})
        elif msg.type in ("voice", "file", "video"):
            parts.append({"type": "text", "text": f"[{msg.type} message received]"})
        else:
            parts.append({"type": "text", "text": text})

        await bot.send_typing(msg.user_id)

        def worker():
            try:
                reply = agent.run_parts(parts)
            except Exception as e:
                reply = f"[ssr error] {e}"

            # Reply natively requires async, so we'd actually need an asyncio task here
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(bot.reply(msg, reply))
            except Exception:
                # If no loop in this thread, create one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(bot.reply(msg, reply))

        threading.Thread(target=worker, daemon=True).start()

    bot.login()
    print("SSR WeChat long-connection starting...")
    bot.run()
