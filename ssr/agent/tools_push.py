"""Agent tool for pushing notifications to channels."""

from __future__ import annotations

import logging
from pathlib import Path
from ssr.config import Settings
from ssr.channels.registry import registry
from ssr.channels.message_parser import parse_and_process_message

logger = logging.getLogger(__name__)

def push_notification_impl(settings: Settings, channel_name: str, target: str, message: str) -> str:
    """Send a notification to a specific channel (e.g. feishu, wechat) and target user/chat.
    
    Args:
        channel_name: Name of the channel ('feishu' or 'wechat').
        target: The target chat ID, user ID, or context representation.
        message: The message text to send.
    """
    import ssr.channels  # trigger registration
    
    channel = registry.get(channel_name)
    if not channel:
        return f"ERROR: Channel '{channel_name}' not found. Registered channels: {', '.join(c.name for c in registry.list_channels())}"

    # XiaoAI speaker is voice-only: speak the text via TTS (it can't send media).
    if channel_name.lower() == "xiaomi" and hasattr(channel, "send_tts"):
        import re
        text = re.sub(r"<ssr_reply_(?:image|files)>.*?</ssr_reply_(?:image|files)>",
                      "", message, flags=re.DOTALL).strip()
        return channel.send_tts(settings, text or message)

    try:
        if hasattr(channel, "load_config"):
            channel.load_config(settings)
        elif channel_name.lower() == "feishu" and getattr(channel, "api", None) is None:
            from ssr.integrations.feishu import load_config as load_fs_cfg, _import_lark
            cfg = load_fs_cfg(settings)
            if cfg:
                lark = _import_lark()
                channel.api = lark.Client.builder().app_id(cfg.app_id).app_secret(cfg.app_secret).build()
        
        # Parse <ssr_reply_image> / <ssr_reply_files> tags: upload files/images
        # via channel.send_file / channel.send_image, then send remaining text.
        # Resolve relative paths against the project directory.
        base_dir = getattr(settings, "project_dir", None) or str(Path.cwd())
        clean_text = parse_and_process_message(channel, target, message, base_dir=base_dir)
        if clean_text:
            channel.send_message(target, clean_text)
        return f"Notification sent successfully via {channel_name} to {target}."
    except Exception as e:
        return f"ERROR sending notification: {e}"
