"""``xiaomi-speaker`` command-line entry point for quick testing.

    xiaomi-speaker login --browser                 # one-time browser login, caches token
    xiaomi-speaker login --pass-token X --user-id Y  # import a token harvested elsewhere
    xiaomi-speaker devices                          # list speakers on the account
    xiaomi-speaker listen [--speaker-name NAME]      # print new ASR utterances live
    xiaomi-speaker speak "text" [--speaker-name NAME]  # one-off TTS takeover
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .speaker import XiaomiSpeaker, XiaomiSpeakerConfig
from .token_store import default_token_path


def _config_from_args(args) -> XiaomiSpeakerConfig:
    return XiaomiSpeakerConfig(
        account=getattr(args, "account", "") or "",
        password=getattr(args, "password", "") or "",
        server_country=getattr(args, "region", "cn") or "cn",
        mina_device_id=getattr(args, "device_id", "") or "",
        speaker_name=getattr(args, "speaker_name", "") or "",
    )


def cmd_login(args) -> int:
    token_path = args.token_path or default_token_path()
    if args.browser:
        from .browser_auth import extract_token

        pass_token, user_id = extract_token(timeout=args.timeout, save_to=token_path)
        print(f"OK — cached token for userId={user_id} at {token_path}")
        return 0
    if args.pass_token and args.user_id:
        from .token_store import import_pass_token

        import_pass_token(token_path, args.pass_token, args.user_id)
        print(f"OK — imported token at {token_path}")
        return 0
    print("Specify --browser, or both --pass-token and --user-id.", file=sys.stderr)
    return 1


def cmd_devices(args) -> int:
    async def run():
        speaker = XiaomiSpeaker(_config_from_args(args), token_path=args.token_path)
        await speaker.connect()
        try:
            for d in await speaker.list_devices():
                marker = " (selected)" if d.device_id == speaker.device.device_id else ""
                print(f"{d.name!r}  deviceID={d.device_id}  hardware={d.hardware}  did={d.did}{marker}")
        finally:
            await speaker.close()

    asyncio.run(run())
    return 0


def cmd_listen(args) -> int:
    async def run():
        speaker = XiaomiSpeaker(_config_from_args(args), token_path=args.token_path)
        await speaker.connect()
        print(f"Listening on {speaker.device.name!r}. Speak to it now (Ctrl-C to stop)…")
        try:
            async for request_id, question in speaker.listen_asr(poll_interval=args.interval):
                print(f"[{request_id}] {question}")
        finally:
            await speaker.close()

    asyncio.run(run())
    return 0


def cmd_speak(args) -> int:
    async def run():
        speaker = XiaomiSpeaker(_config_from_args(args), token_path=args.token_path)
        await speaker.connect()
        try:
            await speaker.speak(args.text)
            print("Spoken.")
        finally:
            await speaker.close()

    asyncio.run(run())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xiaomi-speaker", description=__doc__)
    p.add_argument("--token-path", default=None, help="token file (default: ~/.xiaomi_speaker_sdk/token.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--account", default="", help="Mi account (only needed for first login)")
        sp.add_argument("--password", default="", help="Mi password (only needed for first login)")
        sp.add_argument("--region", default="cn", help="cn/de/i2/ru/sg/us")
        sp.add_argument("--device-id", default="", help="exact MiNA deviceID to select")
        sp.add_argument("--speaker-name", default="", help="substring of the speaker's name to select")

    sp_login = sub.add_parser("login", help="obtain and cache a token")
    sp_login.add_argument("--browser", action="store_true", help="open a browser to log in interactively")
    sp_login.add_argument("--pass-token", default="")
    sp_login.add_argument("--user-id", default="")
    sp_login.add_argument("--timeout", type=float, default=300.0)
    sp_login.set_defaults(func=cmd_login)

    sp_devices = sub.add_parser("devices", help="list speakers on this Mi account")
    add_common(sp_devices)
    sp_devices.set_defaults(func=cmd_devices)

    sp_listen = sub.add_parser("listen", help="print new ASR utterances live")
    add_common(sp_listen)
    sp_listen.add_argument("--interval", type=float, default=1.0)
    sp_listen.set_defaults(func=cmd_listen)

    sp_speak = sub.add_parser("speak", help="take over the speaker and speak text")
    add_common(sp_speak)
    sp_speak.add_argument("text")
    sp_speak.set_defaults(func=cmd_speak)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
