"""Global user-memory ⇆ cloud preferences sync (persist-vault pairing).

When this agent is paired with a persist-vault account (``ssr login`` wrote
``~/.ssr/cloud.json`` → ``settings.cloud_account_token`` + ``cloud_api_base``):

* **on startup** — pull the latest integrated **user preferences** from the cloud
  and write them to ``~/.ssr/profile.md`` (a recognised *configurations* context
  doc), so the agent starts preference-aware; also subscribe to live
  ``memory.preferences`` bus events to refresh them.
* **periodically** — push this machine's **global** memory (``~/.ssr/memory.md``)
  to the cloud, where the cloud agent digests + integrates it into the durable
  user-preferences profile.

Everything degrades gracefully when unpaired or offline.
"""

from __future__ import annotations

import os
import threading

_started = False
_lock = threading.Lock()

DEFAULT_INTERVAL = 1800   # 30 min between pushes


def maybe_start_memory_sync(agent) -> None:
    """Start the sync loop once per process if this agent is cloud-paired."""
    global _started
    settings = agent.settings
    token = getattr(settings, "cloud_account_token", None)
    api_base = getattr(settings, "cloud_api_base", None)
    if not token or not api_base:
        return
    with _lock:
        if _started:
            return
        _started = True
    start_memory_sync(agent)


def start_memory_sync(agent) -> None:
    settings = agent.settings
    token = getattr(settings, "cloud_account_token", None)
    api_base = getattr(settings, "cloud_api_base", None)
    if not token or not api_base:
        return

    # Pull current preferences now, and keep them fresh from live bus updates.
    _pull_preferences(settings, api_base, token)
    try:
        agent.bus.subscribe(
            "memory.preferences",
            lambda ev: _write_preferences(settings, (ev.payload or {}).get("preferences", "")),
            description="cloud preferences refresh",
        )
    except Exception:
        pass

    interval = _env_int("SSR_MEMORY_SYNC_INTERVAL", DEFAULT_INTERVAL)

    def loop() -> None:
        import time

        # First push shortly after startup, then on the interval.
        time.sleep(min(60, interval))
        while True:
            try:
                _push_memory(agent, api_base, token)
            except Exception as e:  # pragma: no cover - network
                print(f"[memory-sync] push failed: {e}")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="memory-sync").start()


def _push_memory(agent, api_base: str, token: str) -> None:
    import httpx

    digest = agent.memory.recall("global") or ""
    if not digest.strip():
        return
    with httpx.Client(timeout=30) as c:
        c.post(
            f"{api_base.rstrip('/')}/api/account/memory",
            headers={"Authorization": f"Bearer {token}"},
            json={"digest": digest[:40000]},
        )


def _pull_preferences(settings, api_base: str, token: str) -> None:
    import httpx

    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(
                f"{api_base.rstrip('/')}/api/account/preferences",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                _write_preferences(settings, (r.json() or {}).get("preferences", ""))
    except Exception as e:  # pragma: no cover - network
        print(f"[memory-sync] pull failed: {e}")


def _write_preferences(settings, text: str) -> None:
    if not text or not str(text).strip():
        return
    try:
        path = settings.home / "profile.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 用户偏好（云端整合，自动同步）\n\n" + str(text).strip() + "\n", encoding="utf-8")
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
