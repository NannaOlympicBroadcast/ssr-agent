"""Unified channels interface (Feishu, WeChat, XiaoAI speaker)."""

from __future__ import annotations

from ssr.channels.base import AbstractChannel
from ssr.channels.registry import registry
from ssr.channels.feishu_channel import FeishuChannel
from ssr.channels.wechat_channel import WeChatChannel
from ssr.channels.xiaomi_channel import XiaomiChannel

# Register all built-in channels
registry.register(FeishuChannel())
registry.register(WeChatChannel())
registry.register(XiaomiChannel())
