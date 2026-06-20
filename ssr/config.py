"""Configuration & environment management for SSR Agent.

Resolves the ``~/.ssr`` home, loads ``~/.ssr/.env`` and per-project config, and
exposes a single :class:`Settings` object used throughout the system.

Required env (configured in ``~/.ssr/.env``):
    GEMINI_API_KEY   API key for the Gemini model.
    TAVILY_API_KEY   API key for Tavily web search.
    DEFAULT_MODEL    Model id, defaults to ``gemini-3.1-flash-lite``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


DEFAULT_MODEL_FALLBACK = "gemini-3.1-flash-lite"

# The set of skill source directories the system scans, in priority order.
SKILL_DIRS = (
    "~/.agent/skills",
    "~/.codex/skills",
    "~/.gemini/skills",
    "~/.claude/skills",
    "~/.ssr/skills",
)

# Configuration documents loaded into the "configurations" context pool.
CONFIG_DOC_NAMES = ("claude.md", "CLAUDE.md", "soul.md", "profile.md")

# Reference-material catalogue loaded into the "refs" context pool. A REFS.md is
# a markdown table describing reference materials (name / location / content).
REFS_DOC_NAMES = ("REFS.md", "refs.md")


def ssr_home() -> Path:
    """Return the ``~/.ssr`` home directory, honouring ``SSR_HOME`` override."""
    raw = os.environ.get("SSR_HOME", "~/.ssr")
    return Path(raw).expanduser()


@dataclass
class Settings:
    """Resolved runtime configuration."""

    home: Path
    project_dir: Path
    gemini_api_key: str | None = None
    tavily_api_key: str | None = None
    default_model: str = DEFAULT_MODEL_FALLBACK
    bus_url: str | None = None  # remote bus server, e.g. ws://host:8765 (SSR_BUS_URL)
    extra: dict[str, str] = field(default_factory=dict)

    # --- derived paths -----------------------------------------------------
    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @property
    def indexes_dir(self) -> Path:
        return self.home / "indexes"

    @property
    def mcp_config(self) -> Path:
        return self.home / "mcp.json"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def memory_file(self) -> Path:
        return self.home / "memory.md"

    @property
    def project_memory_file(self) -> Path:
        return self.project_dir / "memory.md"

    @property
    def project_state_dir(self) -> Path:
        return self.project_dir / ".ssr"

    @property
    def past_chats_file(self) -> Path:
        return self.project_state_dir / "past_chats.jsonl"

    @property
    def feishu_config(self) -> Path:
        return self.home / "feishu.json"

    @property
    def refs_file(self) -> Path:
        return self.home / "REFS.md"

    @property
    def project_refs_file(self) -> Path:
        return self.project_dir / "REFS.md"

    @property
    def rules_file(self) -> Path:
        return self.home / "rules.md"

    @property
    def project_rules_file(self) -> Path:
        return self.project_state_dir / "rules.md"

    @property
    def models_config(self) -> Path:
        return self.home / "models.json"

    @property
    def hooks_config(self) -> Path:
        return self.home / "hooks.json"

    @property
    def project_hooks_config(self) -> Path:
        return self.project_state_dir / "hooks.json"

    @property
    def plugins_dir(self) -> Path:
        return self.home / "plugins"

    @property
    def project_plugins_dir(self) -> Path:
        return self.project_state_dir / "plugins"

    def skill_dirs(self) -> list[Path]:
        # External ecosystems use fixed ~ paths; the SSR entry follows this
        # instance's home (so SSR_HOME / tests resolve correctly).
        dirs = [Path(p).expanduser() for p in SKILL_DIRS if not p.endswith(".ssr/skills")]
        dirs.append(self.skills_dir)
        return dirs

    def ensure_dirs(self) -> None:
        for d in (self.home, self.indexes_dir, self.skills_dir, self.project_state_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings(project_dir: str | os.PathLike | None = None) -> Settings:
    """Load settings from ``~/.ssr/.env`` plus the ambient environment."""
    home = ssr_home()
    home.mkdir(parents=True, exist_ok=True)

    env_path = home / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    proj = Path(project_dir).expanduser().resolve() if project_dir else Path.cwd()

    settings = Settings(
        home=home,
        project_dir=proj,
        gemini_api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
        tavily_api_key=os.environ.get("TAVILY_API_KEY"),
        default_model=os.environ.get("DEFAULT_MODEL", DEFAULT_MODEL_FALLBACK),
        bus_url=os.environ.get("SSR_BUS_URL") or None,
    )
    # Make the key visible to google-genai / google-adk which look up GOOGLE_API_KEY.
    if settings.gemini_api_key:
        os.environ.setdefault("GOOGLE_API_KEY", settings.gemini_api_key)
        os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)
    return settings


def missing_required(settings: Settings) -> list[str]:
    """Return the list of required env vars that are not configured."""
    missing = []
    if not settings.gemini_api_key:
        missing.append("GEMINI_API_KEY")
    if not settings.tavily_api_key:
        missing.append("TAVILY_API_KEY")
    return missing
