"""xiaomi_speaker_sdk — a standalone SDK for XiaoAI (小爱音箱) speakers.

Given a Mi cloud token (extracted once from a browser, or a cached
account/password login), lets your program:

* **listen** for new ASR utterances spoken to the speaker (`XiaomiSpeaker.listen_asr` /
  `.listen`), and
* **take over** the speaker to speak arbitrary text via TTS (`XiaomiSpeaker.speak`).

Quick start::

    from xiaomi_speaker_sdk import XiaomiSpeaker, XiaomiSpeakerConfig
    from xiaomi_speaker_sdk.browser_auth import extract_token
    from xiaomi_speaker_sdk.token_store import default_token_path

    # One-time: open a browser, log in, cache the token.
    extract_token(save_to=default_token_path())

    async def main():
        speaker = XiaomiSpeaker(XiaomiSpeakerConfig(speaker_name="客厅"))
        await speaker.connect()
        await speaker.speak("SDK 已接管这个音箱。")
        async for request_id, question in speaker.listen_asr():
            print("heard:", question)
"""

from .exceptions import (
    LoginRequired,
    SafetyVerificationRequired,
    XiaomiSpeakerError,
    XiaomiUnavailable,
)
from .speaker import SpeakerDevice, XiaomiSpeaker, XiaomiSpeakerConfig

__all__ = [
    "XiaomiSpeaker",
    "XiaomiSpeakerConfig",
    "SpeakerDevice",
    "XiaomiSpeakerError",
    "XiaomiUnavailable",
    "LoginRequired",
    "SafetyVerificationRequired",
]

__version__ = "0.1.0"
