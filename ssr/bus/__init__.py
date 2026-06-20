"""SSR bus — asynchronous event bus for agents and tasks.

The bus lets running agents and external programs communicate through structured
events over JSON-RPC. Each agent owns an in-process :class:`MessageBus`; a
:class:`BusServer` can broker events between many peers, and :class:`BusClient`
is the programmatic entry point for external code.
"""

from __future__ import annotations

from .client import BusClient, RemoteBusBridge
from .core import MessageBus
from .events import BusEvent, topic_matches
from .server import BusServer, run_server

__all__ = [
    "BusEvent",
    "topic_matches",
    "MessageBus",
    "BusClient",
    "RemoteBusBridge",
    "BusServer",
    "run_server",
]
