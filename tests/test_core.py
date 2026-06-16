"""Smoke tests that exercise the offline-safe parts of SSR Agent."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ssr.config import Settings
from ssr.context_pool.index import Embedder, VectorIndex
from ssr.context_pool.loaders import build_pool
from ssr.context_pool.pool import ContextCategory, ContextItem, ContextPool
from ssr.context_pool.retrieval import RetrievalMode, Retriever
from ssr.skills.manager import discover_skills, install_builtin_skills


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    s = Settings(home=home, project_dir=proj, default_model="gemini-3-flash-lite")
    s.ensure_dirs()
    return s


def test_pool_categories():
    pool = ContextPool()
    pool.add(ContextItem(ContextCategory.MEMORY, "m", "hello world"))
    pool.add(ContextItem(ContextCategory.SKILLS, "s", "a skill"))
    assert len(pool) == 2
    assert pool.categories_summary()["memory"] == 1
    assert pool.by_category("skills")[0].title == "s"


def test_category_coerce():
    assert ContextCategory.coerce("TOOLS") is ContextCategory.TOOLS
    assert RetrievalMode.coerce("grep") is RetrievalMode.CLASSIC
    assert RetrievalMode.coerce("embedding") is RetrievalMode.EMBEDDING


def test_embedder_and_index(settings: Settings):
    emb = Embedder()
    idx = VectorIndex(settings.indexes_dir, emb)
    items = [
        ContextItem(ContextCategory.MEMORY, "python", "how to write python tests with pytest"),
        ContextItem(ContextCategory.MEMORY, "cooking", "a recipe for tomato pasta sauce"),
    ]
    n = idx.build("memory", items)
    assert n == 2
    hits = idx.search("memory", "unit testing in python", top_k=1)
    assert hits and hits[0][1]["title"] == "python"


def test_builtin_skill_discovery(settings: Settings):
    installed = install_builtin_skills(settings.skills_dir)
    assert "larksuite" in installed and "agent-browser" in installed
    skills = discover_skills([settings.skills_dir])
    names = {s.name for s in skills}
    assert {"larksuite", "agent-browser"} <= names


def test_retriever_classic_and_embedding(settings: Settings):
    (settings.home / "memory.md").write_text("# mem\n- the deploy key lives in vault\n", "utf-8")
    (settings.project_dir / "claude.md").write_text("Project uses ruff for linting.\n", "utf-8")
    retr = Retriever(settings, tool_specs=[{"name": "read_file", "description": "read a file"}])
    retr.reindex()
    classic = retr.search("ruff", mode="classic", top_k=3)
    assert any("ruff" in r.snippet for r in classic)
    emb = retr.search("how is linting configured", mode="embedding", top_k=3)
    assert emb  # returns something


def test_build_pool_smoke(settings: Settings):
    install_builtin_skills(settings.skills_dir)
    (settings.home / "soul.md").write_text("be kind\n", "utf-8")
    pool = build_pool(settings, tool_specs=[{"name": "t", "description": "d"}])
    summary = pool.categories_summary()
    assert summary["tools"] == 1
    assert summary["skills"] >= 2
    assert summary["configurations"] >= 1
