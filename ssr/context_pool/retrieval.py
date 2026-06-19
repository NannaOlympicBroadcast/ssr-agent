"""Unified retrieval over the context pool: classic (grep) + embedding search."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from ..config import Settings
from .index import Embedder, VectorIndex
from .loaders import build_pool
from .pool import ContextCategory, ContextPool


class RetrievalMode(str, enum.Enum):
    CLASSIC = "classic"      # grep / substring / regex match
    EMBEDDING = "embedding"  # model2vec vector similarity

    @classmethod
    def coerce(cls, value: "str | RetrievalMode") -> "RetrievalMode":
        if isinstance(value, RetrievalMode):
            return value
        v = str(value).strip().lower()
        if v in ("grep", "classic", "string", "keyword"):
            return cls.CLASSIC
        return cls.EMBEDDING


@dataclass
class RetrievalResult:
    score: float
    category: str
    title: str
    source: str
    snippet: str

    def render(self) -> str:
        return f"[{self.score:.3f}] ({self.category}) {self.title} — {self.source}\n    {self.snippet}"


class Retriever:
    """Builds/refreshes indexes and retrieves context items.

    This is the engine behind both the ``/index`` slash command and the agent's
    ``search_context`` tool.
    """

    def __init__(self, settings: Settings, tool_specs: list[dict] | None = None):
        self.settings = settings
        self.tool_specs = tool_specs or []
        self.embedder = Embedder(model_name=settings.default_model and "minishlab/potion-base-8M")
        self.vindex = VectorIndex(settings.indexes_dir, self.embedder)
        self._pool: ContextPool | None = None

    # --- pool management ---------------------------------------------------
    def pool(self, refresh: bool = False) -> ContextPool:
        if self._pool is None or refresh:
            self._pool = build_pool(self.settings, self.tool_specs)
        return self._pool

    def reindex(self, categories: list[str] | None = None) -> dict[str, int]:
        """(Re)build the embedding index for the given categories (or all)."""
        pool = self.pool(refresh=True)
        cats = categories or [c.value for c in ContextCategory]
        counts: dict[str, int] = {}
        for cat in cats:
            items = pool.by_category(cat)
            counts[cat] = self.vindex.build(cat, items)
        return counts

    def index_status(self) -> dict[str, dict]:
        return self.vindex.status()

    # --- retrieval ---------------------------------------------------------
    def search(
        self,
        query: str,
        categories: list[str] | None = None,
        mode: "str | RetrievalMode" = RetrievalMode.EMBEDDING,
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        mode = RetrievalMode.coerce(mode)
        cats = categories or [c.value for c in ContextCategory]
        if mode is RetrievalMode.CLASSIC:
            return self._grep(query, cats, top_k)
        return self._embedding(query, cats, top_k)

    def _embedding(self, query: str, cats: list[str], top_k: int) -> list[RetrievalResult]:
        results: list[RetrievalResult] = []
        for cat in cats:
            hits = self.vindex.search(cat, query, top_k=top_k)
            if not hits:
                # Index missing/stale → build on demand then retry.
                self.reindex([cat])
                hits = self.vindex.search(cat, query, top_k=top_k)
            for score, rec in hits:
                results.append(
                    RetrievalResult(
                        score=score,
                        category=rec.get("category", cat),
                        title=rec.get("title", ""),
                        source=rec.get("source", ""),
                        snippet=_snippet(rec.get("text", "")),
                    )
                )
        results.sort(key=lambda r: -r.score)
        return results[:top_k]

    def _grep(self, query: str, cats: list[str], top_k: int) -> list[RetrievalResult]:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
        results: list[RetrievalResult] = []
        for item in self.pool().items:
            if getattr(item.category, 'value', item.category) not in cats:
                continue
            haystack = f"{item.title}\n{item.text}"
            matches = list(pattern.finditer(haystack))
            if matches:
                results.append(
                    RetrievalResult(
                        score=float(len(matches)),
                        category=getattr(item.category, 'value', item.category),
                        title=item.title,
                        source=item.source,
                        snippet=_around(haystack, matches[0].start()),
                    )
                )
        results.sort(key=lambda r: -r.score)
        return results[:top_k]


def _snippet(text: str, limit: int = 280) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _around(text: str, pos: int, width: int = 160) -> str:
    start = max(0, pos - width // 2)
    end = min(len(text), pos + width // 2)
    return ("…" if start else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")
