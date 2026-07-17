# xiaomi-speaker-sdk

A standalone SDK for XiaoAI (小爱音箱) speakers. Give it one Mi cloud token and
your program can:

- **listen** for new ASR (语音识别) utterances spoken to the speaker, and
- **take over** the speaker to speak arbitrary text back via TTS.

It has no dependency on the rest of ssr-agent — it's a plain, pip-installable
package built on [`miservice_fork`](https://pypi.org/project/miservice-fork/)
(the same Mi cloud protocol wrapper ssr-agent's XiaoAI channel uses).

## Why a separate token flow?

XiaoAI's ASR/TTS APIs need a Mi **passport** `passToken` (the same credential
your phone's Mi Home app uses), not the MIoT open-platform OAuth2 token used
for smart-home device control (the kind tools like miot-desktop cache in
`~/.miot_cache`). Getting a passToken with a plain password login often trips
the account's safety verification (SMS / app confirm), which a headless
script can't complete on its own.

This SDK gets one two ways:

1. **Browser extraction (recommended)** — `browser_auth.extract_token()`
   opens a real, visible Chrome/Edge/Chromium at the Mi login page. You sign
   in normally, completing any verification challenge yourself; the SDK then
   reads the resulting `passToken`/`userId` straight out of the browser's
   cookies via the DevTools Protocol — no copy/paste needed.
2. **Import a token you already have** — if you've extracted a `passToken` +
   `userId` some other way (e.g. via the **miot-desktop** "小爱音箱" tool,
   which does the same browser flow and shows you both values), call
   `token_store.import_pass_token()` directly.

Once a token is cached, every future connection is silent — no browser, no
password.

## Install

```bash
pip install xiaomi-speaker-sdk           # from this directory: pip install .
pip install "xiaomi-speaker-sdk[browser-login]"  # + browser token extraction
```

## Quick start

```python
import asyncio
from xiaomi_speaker_sdk import XiaomiSpeaker, XiaomiSpeakerConfig
from xiaomi_speaker_sdk.browser_auth import extract_token
from xiaomi_speaker_sdk.token_store import default_token_path

# One-time: open a browser, log in, cache the token.
extract_token(save_to=default_token_path())

async def main():
    speaker = XiaomiSpeaker(XiaomiSpeakerConfig(speaker_name="客厅"))
    await speaker.connect()
    await speaker.speak("SDK 已接管这个音箱，请对我说话。")
    async for request_id, question in speaker.listen_asr():
        print("heard:", question)
        await speaker.speak(f"你说了：{question}")

asyncio.run(main())
```

Or from a token extracted elsewhere (e.g. the miot-desktop tool):

```python
from xiaomi_speaker_sdk.token_store import import_pass_token, default_token_path

import_pass_token(default_token_path(), pass_token="<from miot-desktop>", user_id="<from miot-desktop>")
```

## CLI

```bash
xiaomi-speaker login --browser                    # one-time interactive login
xiaomi-speaker login --pass-token X --user-id Y    # import a token from elsewhere
xiaomi-speaker devices                             # list speakers on the account
xiaomi-speaker listen --speaker-name 客厅            # print new ASR utterances live
xiaomi-speaker speak "你好" --speaker-name 客厅       # one-off TTS takeover
```

## API

- `XiaomiSpeakerConfig(account="", password="", server_country="cn", mina_device_id="", speaker_name="", hardware="", tts_command="")`
  — selects which speaker on the account to use (`mina_device_id` is exact,
  `speaker_name` is a substring match; if both are empty the first speaker on
  the account is used). `account`/`password` are only needed for a first-time
  password login — prefer the browser flow instead.
- `XiaomiSpeaker(config, token_path=None)` — `token_path` defaults to
  `~/.xiaomi_speaker_sdk/token.json`.
  - `await speaker.connect()` / `await speaker.close()` (or use `async with`)
  - `await speaker.list_devices()` — every speaker on the account
  - `await speaker.latest_ask()` — `(request_id, question)` or `None`
  - `async for request_id, question in speaker.listen_asr(poll_interval=1.0)` —
    yields each *new* utterance forever
  - `await speaker.listen(callback)` — convenience runner calling
    `await callback(request_id, question)` per utterance
  - `await speaker.speak(text)` — pauses playback and speaks `text` (chunked,
    Markdown stripped, MiIO `play-text` preferred over MiNA `text_to_speech`
    per-hardware)

See `examples/echo_bot.py` and `examples/webhook_forwarder.py` for complete,
runnable bots.

## Multiple speakers

`XiaomiSpeaker` connects to *one* speaker per instance. To handle several,
create one instance per speaker (they share the same cached token file) and
run their `listen_asr()` loops concurrently, e.g. with `asyncio.gather`.

## Region

`server_country` defaults to `"cn"`. If your Mi account is registered on
another Mi cloud region, set it to `de`/`i2`/`ru`/`sg`/`us` to match.
