import json
import tempfile
from pathlib import Path
from ssr.config import Settings
from ssr.context_pool.loaders import load_plugins, build_pool
from ssr.context_pool.pool import ContextCategory


def test_load_plugins_discovery():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        settings.project_state_dir.mkdir(parents=True, exist_ok=True)
        
        # Create global plugins directory
        global_plugins = settings.plugins_dir
        global_plugins.mkdir(parents=True, exist_ok=True)
        
        # Add a claude-plugin in global
        p1 = global_plugins / "my-claude-plugin" / ".claude-plugin"
        p1.mkdir(parents=True, exist_ok=True)
        manifest1 = {"name": "my-claude-plugin", "description": "A test Claude plugin"}
        (p1 / "plugin.json").write_text(json.dumps(manifest1), encoding="utf-8")
        
        # Create project plugins directory
        project_plugins = settings.project_plugins_dir
        project_plugins.mkdir(parents=True, exist_ok=True)
        
        # Add a codex-plugin in project
        p2 = project_plugins / "my-codex-plugin" / ".codex-plugin"
        p2.mkdir(parents=True, exist_ok=True)
        manifest2 = {"name": "my-codex-plugin", "description": "A test Codex plugin"}
        (p2 / "plugin.json").write_text(json.dumps(manifest2), encoding="utf-8")
        
        # Load plugins
        items = load_plugins(settings)
        
        assert len(items) == 2
        
        # Check metadata and category
        # Sort them by title to be deterministic
        items = sorted(items, key=lambda x: x.title)
        
        assert items[0].category == ContextCategory.SKILLS
        assert items[0].title == "plugin my-claude-plugin"
        assert items[0].metadata == {"kind": "plugin"}
        assert json.loads(items[0].text) == manifest1
        
        assert items[1].category == ContextCategory.SKILLS
        assert items[1].title == "plugin my-codex-plugin"
        assert items[1].metadata == {"kind": "plugin"}
        assert json.loads(items[1].text) == manifest2


def test_build_pool_with_plugins():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        settings.project_state_dir.mkdir(parents=True, exist_ok=True)
        
        # Add a plugin to project state dir
        p = settings.project_plugins_dir / "my-plugin" / ".claude-plugin"
        p.mkdir(parents=True, exist_ok=True)
        manifest = {"name": "my-plugin", "description": "A plugin"}
        (p / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        
        # Build context pool
        pool = build_pool(settings)
        
        # Retrieve context items under SKILLS category
        skills = pool.by_category(ContextCategory.SKILLS)
        
        # Check if the plugin is in the list
        plugin_items = [item for item in skills if item.metadata.get("kind") == "plugin"]
        assert len(plugin_items) == 1
        assert plugin_items[0].title == "plugin my-plugin"
        assert json.loads(plugin_items[0].text) == manifest
