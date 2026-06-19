"""Abstract base class for channels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from ssr.config import Settings

class AbstractChannel(ABC):
    name: str

    @abstractmethod
    def configure(self, settings: Settings) -> None:
        """Interactive console configuration."""
        pass

    @abstractmethod
    def serve(self, settings: Settings) -> None:
        """Start listening / long polling on the channel."""
        pass

    @abstractmethod
    def send_message(self, target: str, text: str) -> None:
        """Send a text message to a chat or user."""
        pass

    @abstractmethod
    def send_file(self, target: str, path: str, mime_type: str) -> None:
        """Send a local file to a chat or user."""
        pass

    @abstractmethod
    def send_image(self, target: str, path_or_bytes: str | bytes) -> None:
        """Send a local image file or raw image bytes to a chat or user."""
        pass
