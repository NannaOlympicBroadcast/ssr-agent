"""Minimal example: listen for ASR and echo the question back via TTS.

Run once to log in (opens a browser):
    python -m xiaomi_speaker_sdk.cli login --browser

Then:
    python examples/echo_bot.py [speaker-name-substring]
"""

from __future__ import annotations

import asyncio
import sys

from xiaomi_speaker_sdk import XiaomiSpeaker, XiaomiSpeakerConfig


async def main() -> None:
    speaker_name = sys.argv[1] if len(sys.argv) > 1 else ""
    speaker = XiaomiSpeaker(XiaomiSpeakerConfig(speaker_name=speaker_name))
    await speaker.connect()
    print(f"Connected to {speaker.device.name!r}. Speak to it now (Ctrl-C to stop)…")
    await speaker.speak("回声机器人已接管，请说话。")
    try:
        async for request_id, question in speaker.listen_asr():
            print(f"[{request_id}] heard: {question}")
            await speaker.speak(f"你说了：{question}")
    finally:
        await speaker.close()


if __name__ == "__main__":
    asyncio.run(main())
