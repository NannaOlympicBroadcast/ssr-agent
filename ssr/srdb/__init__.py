"""srdb — the SSR agent debug server.

A per-process TCP server (newline-delimited JSON-RPC 2.0) that exposes every
live agent in the process for inspection and control: agents, sub-agents,
sessions, channels, bus state — plus session/sub-agent/bus editing, direct
channel sends, and arbitrary in-process Python ``eval``. Each agent advertises a
``tcp://host:port?key=…&agent=<id>`` debug link.
"""

from __future__ import annotations

from .client import SrdbClient, SrdbError, parse_link
from .registry import (
    first_agent,
    get_agent,
    list_agents,
    register_agent,
    unregister_agent,
)
from .server import SrdbServer, announce_link, ensure_server

__all__ = [
    "SrdbServer",
    "SrdbClient",
    "SrdbError",
    "parse_link",
    "ensure_server",
    "announce_link",
    "register_agent",
    "unregister_agent",
    "get_agent",
    "list_agents",
    "first_agent",
]
