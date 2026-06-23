"""Process-global registry of live :class:`SSRAgent` instances for srdb.

Every ``SSRAgent`` registers itself here on construction so the per-process srdb
debug server can enumerate and introspect every agent (and sub-agent) running in
the process — channels, the REPL, the gateway "all" channel (which hosts several
agents at once), etc. Entries are held weakly so a garbage-collected agent drops
out of the registry on its own.
"""

from __future__ import annotations

import threading
import weakref

_lock = threading.RLock()
_agents: "weakref.WeakValueDictionary[str, object]" = weakref.WeakValueDictionary()


def register_agent(agent) -> None:
    """Register a live agent by its ``agent_id``."""
    aid = getattr(agent, "agent_id", None)
    if not aid:
        return
    with _lock:
        _agents[aid] = agent


def unregister_agent(agent) -> None:
    with _lock:
        _agents.pop(getattr(agent, "agent_id", ""), None)


def get_agent(agent_id: str):
    with _lock:
        return _agents.get(agent_id)


def list_agents() -> list:
    """All live agents (most-recently-registered order is not guaranteed)."""
    with _lock:
        return list(_agents.values())


def first_agent():
    """Any live agent — the default target when a request omits an agent id."""
    with _lock:
        for a in _agents.values():
            return a
    return None
