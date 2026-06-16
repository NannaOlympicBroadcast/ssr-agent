"""The context pool: the heart of SSR Agent.

Four categories of context are tracked:

* ``tools``           — built-in + MCP tools (configured in ``~/.ssr/mcp.json``)
* ``configurations``  — ``claude.md`` / ``soul.md`` / ``profile.md`` from ``~/.ssr`` and project
* ``skills``          — user skills discovered across the skill directories
* ``memory``          — ``memory.md`` files + ``.ssr/past_chats.jsonl`` conversation log

Each piece of context is a :class:`ContextItem`. Retrieval can be performed with a
classic ``grep`` string match or with a model2vec embedding search.
"""

from .pool import ContextCategory, ContextItem, ContextPool
from .retrieval import RetrievalMode, Retriever

__all__ = [
    "ContextCategory",
    "ContextItem",
    "ContextPool",
    "RetrievalMode",
    "Retriever",
]
