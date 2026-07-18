"""Headed-browser token extraction.

XiaoAI's ASR/TTS APIs need a Mi **passport** ``passToken`` (not the MIoT
open-platform OAuth2 token used for device control). Getting one
programmatically often trips the Mi account's safety verification (SMS / app
confirm), which a headless password login can't complete.

This module opens a real, visible Chrome/Edge/Chromium, lets the user log in
normally (completing any verification challenge themselves), then reads the
resulting ``passToken``/``userId`` cookies straight out of the browser via the
DevTools Protocol (``Storage.getCookies``, which sees httpOnly cookies) —
no manual copy/paste required.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from . import token_store

logger = logging.getLogger("xiaomi_speaker_sdk")

LOGIN_URL = "https://account.xiaomi.com/pass/serviceLogin?sid=micoapi&_locale=zh_CN"


def find_browser() -> str | None:
    """Locate a Chrome/Edge/Chromium executable to drive via DevTools Protocol."""
    import shutil

    for n in ("chrome", "google-chrome", "google-chrome-stable", "chromium",
              "chromium-browser", "msedge"):
        p = shutil.which(n)
        if p:
            return p
    candidates: list[Path] = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
                candidates.append(Path(base) / "Microsoft/Edge/Application/msedge.exe")
    elif sys.platform == "darwin":
        candidates += [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    for c in candidates:
        try:
            if c.exists():
                return str(c)
        except OSError:
            pass
    return None


def _cdp_get_all_cookies(port: int) -> list[dict]:
    """All browser cookies via CDP ``Storage.getCookies`` (includes httpOnly)."""
    import asyncio

    import httpx

    try:
        info = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3.0).json()
    except Exception:
        return []
    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        return []

    async def _go() -> list[dict]:
        import websockets

        async with websockets.connect(ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            for _ in range(50):
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    return msg.get("result", {}).get("cookies", []) or []
            return []

    try:
        return asyncio.run(_go())
    except Exception:
        return []


def _pick_cookie(cookies: list[dict], name: str) -> str | None:
    """Value of cookie ``name`` (preferring a mi.com-scoped one), or None."""
    matches = [c for c in cookies if c.get("name") == name and c.get("value")]
    if not matches:
        return None
    matches.sort(key=lambda c: "mi.com" not in (c.get("domain") or ""))
    return matches[0]["value"]


def extract_token(
    profile_dir: Path | None = None,
    timeout: float = 300.0,
    port: int = 9222,
    save_to: Path | None = None,
) -> tuple[str, str]:
    """Open a headed browser, wait for Mi account login, return ``(pass_token, user_id)``.

    Requires the ``browser-login`` extra (``httpx``, ``websockets``). Raises
    ``RuntimeError`` if no browser is found or the user doesn't finish within
    ``timeout`` seconds. If ``save_to`` is given, the harvested token is also
    written there (see :mod:`xiaomi_speaker_sdk.token_store`) so a later
    ``XiaomiSpeaker(token_path=save_to)`` can use it immediately.
    """
    browser = find_browser()
    if not browser:
        raise RuntimeError(
            "No Chrome/Edge/Chromium found. Install one, or obtain passToken/"
            "userId manually from a logged-in browser's cookies and pass them "
            "to token_store.import_pass_token()."
        )

    profile = Path(profile_dir) if profile_dir else Path.home() / ".xiaomi_speaker_sdk" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        browser,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        LOGIN_URL,
    ]
    print("Opening a browser window — please sign in to your Mi account "
          "(completing SMS/app verification if prompted).")
    proc = subprocess.Popen(args)

    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("Browser was closed before a token could be captured.")
            cookies = _cdp_get_all_cookies(port)
            pass_token = _pick_cookie(cookies, "passToken")
            user_id = _pick_cookie(cookies, "userId")
            if pass_token and user_id:
                print("[+] Captured passToken/userId from the browser.")
                if save_to is not None:
                    token_store.import_pass_token(save_to, pass_token, user_id)
                return pass_token, user_id
            time.sleep(2)
        raise RuntimeError(f"Timed out after {timeout:.0f}s waiting for login.")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
