"""On-disk token storage shared by every entry point in this SDK.

A "token" here is the small JSON blob ``miservice_fork``'s ``MiTokenStore``
persists: ``deviceId``/``userAgent`` (a stable identity for the Mi passport)
plus, once logged in, ``userId``/``passToken`` and a per-service
``(ssecurity, serviceToken)`` pair. The passToken is the long-lived,
account-level credential — once you have it (e.g. harvested from a browser
via :mod:`xiaomi_speaker_sdk.browser_auth`), every future login exchanges it
for a fresh serviceToken with no safety-verification challenge.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def default_token_path() -> Path:
    """``~/.xiaomi_speaker_sdk/token.json`` — override by passing an explicit path."""
    return Path.home() / ".xiaomi_speaker_sdk" / "token.json"


def load_token(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_token(path: Path, token: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, ensure_ascii=False, indent=2), "utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def import_pass_token(path: Path, pass_token: str, user_id: str) -> None:
    """Seed the token store with a browser-obtained ``passToken`` + ``userId``.

    This bypasses password login and safety verification entirely: get the
    values from a logged-in browser at ``account.xiaomi.com`` (DevTools →
    Application/Storage → Cookies → ``passToken``, ``userId``), or use
    :func:`xiaomi_speaker_sdk.browser_auth.extract_token` to do it
    automatically. Any existing ``deviceId``/``userAgent`` is preserved so the
    Mi passport keeps seeing the same "device".
    """
    tok = load_token(path)
    if not tok.get("deviceId"):
        try:
            from miservice.miaccount import get_random

            tok["deviceId"] = get_random(16).upper()
        except Exception:
            import secrets

            tok["deviceId"] = secrets.token_hex(8).upper()
    from .account import account_user_agent

    tok["userAgent"] = account_user_agent(tok["deviceId"])
    tok["passToken"] = pass_token.strip()
    tok["userId"] = str(user_id).strip()
    # Drop any stale per-sid serviceToken so the next login re-derives it.
    tok.pop("micoapi", None)
    save_token(path, tok)


def has_pass_token(path: Path) -> bool:
    return bool(load_token(path).get("passToken"))
