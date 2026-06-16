"""Embedding index management backed by model2vec.

Indexes live in ``~/.ssr/indexes`` as ``<category>.npy`` (float32 matrix) plus a
``<category>.json`` sidecar with item metadata. A tiny deterministic hashing
embedder is used as a fallback when model2vec (or its weights) is unavailable so
the system degrades gracefully offline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from .pool import ContextItem

_DEFAULT_MODEL = "minishlab/potion-base-8M"
_HASH_DIM = 256
_TOKEN_RE = re.compile(r"[a-z0-9一-鿿]+")


class Embedder:
    """Wraps model2vec; falls back to a hashing vectoriser if unavailable."""

    def __init__(self, model_name: str = _DEFAULT_MODEL):
        self.model_name = model_name
        self._model = None
        self.backend = "hash"
        try:
            from model2vec import StaticModel  # type: ignore

            self._model = StaticModel.from_pretrained(model_name)
            self.backend = "model2vec"
        except Exception:
            self._model = None

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self._model is not None:
            vecs = np.asarray(self._model.encode(texts), dtype=np.float32)
        else:
            vecs = np.stack([self._hash_embed(t) for t in texts]).astype(np.float32)
        return _l2_normalize(vecs)

    @property
    def dim(self) -> int:
        if self._model is not None:
            try:
                return int(self._model.dim)
            except Exception:
                return 256
        return _HASH_DIM

    def _hash_embed(self, text: str) -> np.ndarray:
        vec = np.zeros(_HASH_DIM, dtype=np.float32)
        for tok in _TOKEN_RE.findall(text.lower()):
            vec[hash(tok) % _HASH_DIM] += 1.0
        return vec


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class VectorIndex:
    """Per-category on-disk vector index."""

    def __init__(self, indexes_dir: Path, embedder: Embedder | None = None):
        self.dir = indexes_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder or Embedder()

    def _paths(self, category: str) -> tuple[Path, Path]:
        return self.dir / f"{category}.npy", self.dir / f"{category}.json"

    def build(self, category: str, items: list[ContextItem]) -> int:
        """Embed and persist all items for a category. Returns item count."""
        npy, meta = self._paths(category)
        if not items:
            np.save(npy, np.zeros((0, self.embedder.dim), dtype=np.float32))
            meta.write_text(json.dumps({"backend": self.embedder.backend, "items": []}), "utf-8")
            return 0
        texts = [f"{it.title}\n{it.text}" for it in items]
        vecs = self.embedder.encode(texts)
        np.save(npy, vecs)
        meta.write_text(
            json.dumps(
                {"backend": self.embedder.backend, "items": [it.to_dict() for it in items]},
                ensure_ascii=False,
            ),
            "utf-8",
        )
        return len(items)

    def search(self, category: str, query: str, top_k: int = 5) -> list[tuple[float, dict]]:
        npy, meta = self._paths(category)
        if not npy.exists() or not meta.exists():
            return []
        vecs = np.load(npy)
        records = json.loads(meta.read_text("utf-8")).get("items", [])
        if len(records) == 0 or vecs.shape[0] == 0:
            return []
        qv = self.embedder.encode([query])[0]
        scores = vecs @ qv
        order = np.argsort(-scores)[:top_k]
        return [(float(scores[i]), records[i]) for i in order]

    def status(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for meta in self.dir.glob("*.json"):
            try:
                data = json.loads(meta.read_text("utf-8"))
                out[meta.stem] = {
                    "items": len(data.get("items", [])),
                    "backend": data.get("backend", "?"),
                    "mtime": meta.stat().st_mtime,
                }
            except Exception:
                continue
        return out
