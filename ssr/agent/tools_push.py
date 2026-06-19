"""Agent tool for pushing notifications to channels."""

from __future__ import annotations

import logging
from ssr.config import Settings
from ssr.channels.registry import registry

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
        
    try:
        if hasattr(channel, "load_config"):
            channel.load_config(settings)
        elif channel_name.lower() == "feishu" and getattr(channel, "api", None) is None:
            from ssr.integrations.feishu import load_config as load_fs_cfg, _import_lark
            cfg = load_fs_cfg(settings)
            if cfg:
                lark = _import_lark()
                channel.api = lark.Client.builder().app_id(cfg.app_id).app_secret(cfg.app_secret).build()
                
        channel.send_message(target, message)
        return f"Notification sent successfully via {channel_name} to {target}."
    except Exception as e:
        return f"ERROR sending notification: {e}"
