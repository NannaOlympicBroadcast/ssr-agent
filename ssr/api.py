"""Public Python API for embedding SSR Agent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent.core import SSRAgent as _CoreAgent
from .config import Settings, load_settings


@dataclass
class Config:
    cwd: str | None = None
    settings: Settings | None = None


class SSRAgent:
    """Small Python-callable facade.

    Example:
        agent = SSRAgent(Config(cwd="/repo"))
        result = agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
    """

    def __init__(self, config: Config | Settings | dict[str, Any] | None = None):
        if isinstance(config, Settings):
            settings = config
        elif isinstance(config, Config):
            settings = config.settings or load_settings(config.cwd)
        elif isinstance(config, dict):
            settings = load_settings(config.get("cwd"))
        else:
            settings = load_settings(None)
        self._agent = _CoreAgent(settings)

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        messages = request.get("messages", [])
        text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        reply = self._agent.run(text)
        return {"messages": [{"role": "assistant", "content": reply}], "content": reply}
