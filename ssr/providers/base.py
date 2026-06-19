"""Base provider class."""

from __future__ import annotations

import abc
from ssr.models import ModelEntry
from ssr.config import Settings
from ssr.agent.tools import ToolKit

class AbstractProvider(abc.ABC):
    def __init__(self, settings: Settings, entry: ModelEntry, agent_instance=None):
        self.settings = settings
        self.entry = entry
        self.agent_instance = agent_instance

    @property
    def name(self) -> str:
        return self.entry.provider

    @property
    def model_name(self) -> str:
        return self.entry.model

    @abc.abstractmethod
    def complete(
        self,
        contents: list,
        system_instruction: str,
        toolkit: ToolKit,
        max_iters: int,
        tag: str = "ssr",
    ) -> str:
        """Run the chat/agent completion loop with tool calls."""
        pass
