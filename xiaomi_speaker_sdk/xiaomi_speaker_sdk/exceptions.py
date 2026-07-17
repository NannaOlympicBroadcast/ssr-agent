"""Exceptions raised by ``xiaomi_speaker_sdk``."""

from __future__ import annotations


class XiaomiSpeakerError(RuntimeError):
    """Base class for all SDK errors."""


class XiaomiUnavailable(XiaomiSpeakerError):
    """Raised when ``miservice_fork`` / ``aiohttp`` are not installed."""


class LoginRequired(XiaomiSpeakerError):
    """Raised when neither a cached token nor account credentials are available."""


class SafetyVerificationRequired(XiaomiSpeakerError):
    """Raised when the Mi account needs an interactive safety verification.

    ``verification_url`` is the (very long) URL the user must open in a
    browser to complete SMS / app confirmation before automated login works.
    Use :func:`xiaomi_speaker_sdk.browser_auth.extract_token` instead, which
    drives a real browser through this step and harvests the resulting token.
    """

    def __init__(self, message: str, verification_url: str | None = None):
        super().__init__(message)
        self.verification_url = verification_url
