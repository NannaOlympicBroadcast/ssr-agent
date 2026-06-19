"""Channel registry to manage multiple messaging channel integrations."""

from __future__ import annotations

from ssr.channels.base import AbstractChannel

class ChannelRegistry:
    def __init__(self):
        self.channels: dict[str, AbstractChannel] = {}

    def register(self, channel: AbstractChannel) -> None:
        self.channels[channel.name.lower()] = channel

    def get(self, name: str) -> AbstractChannel | None:
        return self.channels.get(name.lower())

    def list_channels(self) -> list[AbstractChannel]:
        return list(self.channels.values())

registry = ChannelRegistry()
