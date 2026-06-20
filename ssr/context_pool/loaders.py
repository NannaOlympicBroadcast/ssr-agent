"""Loaders that populate the four context-pool categories from disk."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import CONFIG_DOC_NAMES, REFS_DOC_NAMES, Settings
from ..skills.manager import discover_skills
from .pool import ContextCategory, ContextItem, ContextPool

_MAX_BYTES = 200_000


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:_MAX_BYTES]
    except OSError:
        return ""


def load_configurations(settings: Settings) -> list[ContextItem]:
    """Load claude.md / soul.md / profile.md from ~/.ssr and the project dir."""
    items: list[ContextItem] = []
    seen: set[Path] = set()
    for base in (settings.home, settings.project_dir):
        for name in CONFIG_DOC_NAMES:
            path = base / name
            if not path.exists() or path in seen:
                continue
            seen.add(path)
            text = _read_text(path)
            if not text.strip():
                continue
            scope = "global" if base == settings.home else "project"
            items.append(
                ContextItem(
                    category=ContextCategory.CONFIGURATIONS,
                    title=f"{name} ({scope})",
                    text=text,
                    source=str(path),
                    metadata={"scope": scope, "name": name.lower()},
                )
            )
    return items


def parse_refs_table(text: str) -> list[dict]:
    """Parse a REFS.md markdown table into a list of reference records.

    The table is expected to have the columns ``名称/Name``, ``位置/Location``
    and ``内容/Content`` (in any order, header names are matched loosely). Rows
    outside the table and the header/separator lines are ignored.
    """
    rows: list[dict] = []
    header: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Skip the GFM separator row ( |---|---| ).
        if all(set(c) <= {"-", ":", " "} and c for c in cells):
            continue
        if header is None:
            header = [c.lower() for c in cells]
            continue
        rec: dict = {}
        for i, c in enumerate(cells):
            key = header[i] if i < len(header) else f"col{i}"
            rec[key] = c
        # Normalise to name/location/content regardless of the header language.
        def pick(*keys: str) -> str:
            for h_i, h in enumerate(header or []):
                if any(k in h for k in keys) and h_i < len(cells):
                    return cells[h_i]
            return ""
        name = pick("名称", "name", "资料")
        if not name:
            continue
        rec["name"] = name
        rec["location"] = pick("位置", "location", "位置/path", "url", "path")
        rec["content"] = pick("内容", "content", "描述", "description")
        rows.append(rec)
    return rows


def load_refs(settings: Settings) -> list[ContextItem]:
    """Load REFS.md (global + project): a catalogue of reference materials.

    The whole file is exposed as one retrievable item per scope, and each table
    row additionally becomes its own item so an individual reference can be
    surfaced by name/location/content.
    """
    items: list[ContextItem] = []
    seen: set[Path] = set()
    for base in (settings.home, settings.project_dir):
        for name in REFS_DOC_NAMES:
            path = base / name
            if not path.exists() or path in seen:
                continue
            seen.add(path)
            text = _read_text(path)
            if not text.strip():
                continue
            scope = "global" if base == settings.home else "project"
            items.append(
                ContextItem(
                    category=ContextCategory.REFS,
                    title=f"REFS.md ({scope})",
                    text=text,
                    source=str(path),
                    metadata={"scope": scope, "kind": "refs"},
                )
            )
            for rec in parse_refs_table(text):
                items.append(
                    ContextItem(
                        category=ContextCategory.REFS,
                        title=f"ref: {rec['name']}",
                        text=f"{rec['name']} — 位置: {rec.get('location', '')}\n{rec.get('content', '')}",
                        source=str(path),
                        metadata={"scope": scope, "kind": "ref", **rec},
                    )
                )
    return items


def load_skills(settings: Settings) -> list[ContextItem]:
    """Discover skills across all configured skill directories."""
    items: list[ContextItem] = []
    for skill in discover_skills(settings.skill_dirs()):
        items.append(
            ContextItem(
                category=ContextCategory.SKILLS,
                title=skill.name,
                text=skill.content,
                source=str(skill.path),
                metadata={"description": skill.description, "dir": str(skill.path.parent)},
            )
        )
    return items


def load_memory(settings: Settings) -> list[ContextItem]:
    """Load memory.md (global + project) and the past_chats.jsonl transcript log."""
    items: list[ContextItem] = []
    for path, scope in ((settings.memory_file, "global"), (settings.project_memory_file, "project")):
        if path.exists():
            text = _read_text(path)
            if text.strip():
                items.append(
                    ContextItem(
                        category=ContextCategory.MEMORY,
                        title=f"memory.md ({scope})",
                        text=text,
                        source=str(path),
                        metadata={"scope": scope, "kind": "memory"},
                    )
                )

    # Conversation history — each turn becomes its own retrievable item.
    chats = settings.past_chats_file
    if chats.exists():
        for n, line in enumerate(chats.read_text(encoding="utf-8", errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = rec.get("role", "?")
            content = rec.get("content", "")
            ts = rec.get("ts", "")
            items.append(
                ContextItem(
                    category=ContextCategory.MEMORY,
                    title=f"chat#{n} {role} {ts}".strip(),
                    text=f"[{role}] {content}",
                    source=str(chats),
                    metadata={"kind": "chat", "role": role, "ts": ts, "line": n},
                )
            )
    return items


def load_tools_context(tool_specs: list[dict]) -> list[ContextItem]:
    """Turn tool specs (name/description) into retrievable context items."""
    items: list[ContextItem] = []
    for spec in tool_specs:
        name = spec.get("name", "tool")
        desc = spec.get("description", "")
        origin = spec.get("origin", "builtin")
        items.append(
            ContextItem(
                category=ContextCategory.TOOLS,
                title=name,
                text=f"{name}: {desc}",
                source=origin,
                metadata={"origin": origin, **{k: v for k, v in spec.items() if k != "description"}},
            )
        )
    return items


def load_plugins(settings: Settings) -> list[ContextItem]:
    """Load Claude-Code-style plugin manifests as skill context."""
    items: list[ContextItem] = []
    for base in (settings.plugins_dir, settings.project_plugins_dir):
        if not base.exists():
            continue
        for manifest in base.glob("*/.claude-plugin/plugin.json"):
            text = _read_text(manifest)
            if text.strip():
                items.append(
                    ContextItem(
                        category=ContextCategory.SKILLS,
                        title=f"plugin {manifest.parent.parent.name}",
                        text=text,
                        source=str(manifest),
                        metadata={"kind": "plugin"},
                    )
                )
        for manifest in base.glob("*/.codex-plugin/plugin.json"):
            text = _read_text(manifest)
            if text.strip():
                items.append(
                    ContextItem(
                        category=ContextCategory.SKILLS,
                        title=f"plugin {manifest.parent.parent.name}",
                        text=text,
                        source=str(manifest),
                        metadata={"kind": "plugin"},
                    )
                )
    return items


def build_pool(settings: Settings, tool_specs: list[dict] | None = None) -> ContextPool:
    """Construct the full context pool from all four categories."""
    pool = ContextPool()
    pool.extend(load_tools_context(tool_specs or []))
    pool.extend(load_configurations(settings))
    pool.extend(load_skills(settings))
    pool.extend(load_plugins(settings))
    pool.extend(load_memory(settings))
    pool.extend(load_refs(settings))
    return pool
