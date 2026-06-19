"""Provider registry and factory."""

from __future__ import annotations

from ssr.config import Settings
from ssr.models import ModelEntry
from ssr.providers.base import AbstractProvider
from ssr.providers.gemini import GeminiProvider

def get_provider(settings: Settings, entry: ModelEntry, agent_instance=None) -> AbstractProvider:
    """Return an AbstractProvider instance based on the ModelEntry provider."""
    provider_name = entry.provider.lower()
    
    if provider_name == "gemini":
        return GeminiProvider(settings, entry, agent_instance)
    elif provider_name == "anthropic":
        from ssr.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(settings, entry, agent_instance)
    elif provider_name == "openai":
        from ssr.providers.openai_provider import OpenAIProvider
        return OpenAIProvider(settings, entry, agent_instance)
    else:
        # Fallback to Gemini
        return GeminiProvider(settings, entry, agent_instance)
