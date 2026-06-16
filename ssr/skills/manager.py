"""Discover skills from ~/.agent, ~/.codex, ~/.gemini, ~/.claude and ~/.ssr.

A *skill* is a directory containing a ``SKILL.md`` (preferred) or any markdown
file. The optional YAML frontmatter ``name`` / ``description`` is honoured.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_MAX_BYTES = 100_000


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    content: str
    origin: str  # which skill dir it came from


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, text[m.end():]


def _load_skill_file(md: Path, origin: str) -> Skill | None:
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")[:_MAX_BYTES]
    except OSError:
        return None
    meta, body = _parse_frontmatter(raw)
    name = meta.get("name") or md.parent.name
    desc = meta.get("description", "")
    if not desc:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                desc = line[:200]
                break
    return Skill(name=name, description=desc, path=md, content=body, origin=origin)


def discover_skills(skill_dirs: list[Path]) -> list[Skill]:
    """Scan all skill directories and return discovered skills (dedup by name)."""
    found: dict[str, Skill] = {}
    for base in skill_dirs:
        if not base.exists():
            continue
        origin = str(base)
        # A skill is a sub-directory with a SKILL.md, or a loose .md file.
        for entry in sorted(base.iterdir()):
            md: Path | None = None
            if entry.is_dir():
                for cand in ("SKILL.md", "skill.md", "README.md"):
                    if (entry / cand).exists():
                        md = entry / cand
                        break
                if md is None:
                    mds = list(entry.glob("*.md"))
                    md = mds[0] if mds else None
            elif entry.suffix.lower() == ".md":
                md = entry
            if md is None:
                continue
            skill = _load_skill_file(md, origin)
            if skill:
                found.setdefault(skill.name, skill)
    return list(found.values())


def install_builtin_skills(target_dir: Path, force: bool = False) -> list[str]:
    """Copy the bundled built-in skills into ``target_dir`` (~/.ssr/skills)."""
    src_root = Path(__file__).resolve().parent.parent / "builtin_skills"
    target_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    if not src_root.exists():
        return installed
    for skill_dir in sorted(src_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        dest = target_dir / skill_dir.name
        if dest.exists() and not force:
            continue
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_dir, dest)
        installed.append(skill_dir.name)
    return installed
