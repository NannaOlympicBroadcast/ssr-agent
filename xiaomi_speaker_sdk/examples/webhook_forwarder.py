"""Forward every new ASR utterance to a webhook URL as JSON.

This is the same pattern the miot-desktop "XiaoAI speaker" tool uses
internally — a minimal standalone version so you can run it from any script
or server without the desktop app.

Usage:
    python examples/webhook_forwarder.py <webhook-url> [speaker-name-substring]
"""

from __future__ import annotations

import asyncio
import sys
import time

import aiohttp

from xiaomi_speaker_sdk import XiaomiSpeaker, XiaomiSpeakerConfig


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    webhook_url = sys.argv[1]
    speaker_name = sys.argv[2] if len(sys.argv) > 2 else ""

    speaker = XiaomiSpeaker(XiaomiSpeakerConfig(speaker_name=speaker_name))
    await speaker.connect()
    print(f"Connected to {speaker.device.name!r}. Forwarding ASR to {webhook_url} …")

    async with aiohttp.ClientSession() as session:
        try:
            async for request_id, question in speaker.listen_asr():
                payload = {
                    "request_id": request_id,
                    "question": question,
                    "device_id": speaker.device.device_id,
                    "device_name": speaker.device.name,
                    "timestamp": int(time.time() * 1000),
                }
                try:
                    async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        print(f"[{request_id}] forwarded (HTTP {r.status}): {question}")
                except Exception as e:
                    print(f"[{request_id}] webhook POST failed: {e}")
        finally:
            await speaker.close()


if __name__ == "__main__":
    asyncio.run(main())
