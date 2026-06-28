import json
import tempfile
from pathlib import Path
from ssr.config import Settings
from ssr.plugins import install_builtin_plugins
from ssr.integrations.mcp_client import (
    MCPManager,
    find_plugin_mcp_configs,
    resolve_dirnames
)
from ssr.agent.core import _load_mcp_specs

def test_install_builtin_plugins():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        
        installed = install_builtin_plugins(settings.plugins_dir)
        # The legacy `miot` plugin has been removed (replaced by the native
        # Miloco integration); `chrome-devtools` remains a bundled plugin.
        assert "miot" not in installed
        assert "chrome-devtools" in installed

        # Check that files were copied
        plugin_dir = settings.plugins_dir / "chrome-devtools"
        assert plugin_dir.exists()
        assert (plugin_dir / ".claude-plugin" / "plugin.json").exists()
        assert (plugin_dir / ".mcp.json").exists()


def test_resolve_dirnames():
    dirname = "/path/to/plugin"
    val = {
        "command": "uv",
        "args": ["run", "${__dirname}/start.py", "abc"],
        "env": {
            "PYTHONPATH": "__dirname;__dirname/lib",
            "ROOT": "${CLAUDE_PLUGIN_ROOT}"
        }
    }
    resolved = resolve_dirnames(val, dirname)
    assert resolved["args"][1] == "/path/to/plugin/start.py"
    assert resolved["env"]["PYTHONPATH"] == "/path/to/plugin;/path/to/plugin/lib"
    assert resolved["env"]["ROOT"] == "/path/to/plugin"


def test_find_plugin_mcp_configs():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        settings.project_state_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Create a plugin with inline mcpServers in plugin.json
        p1 = settings.plugins_dir / "inline-plugin"
        p1_manifest = p1 / ".claude-plugin"
        p1_manifest.mkdir(parents=True, exist_ok=True)
        manifest1 = {
            "name": "inline-plugin",
            "mcpServers": {
                "inline-server": {
                    "command": "node",
                    "args": ["${__dirname}/index.js"]
                }
            }
        }
        (p1_manifest / "plugin.json").write_text(json.dumps(manifest1), encoding="utf-8")
        
        # 2. Create a plugin with external .mcp.json
        p2 = settings.project_plugins_dir / "ext-plugin"
        p2_manifest = p2 / ".codex-plugin"
        p2_manifest.mkdir(parents=True, exist_ok=True)
        manifest2 = {
            "name": "ext-plugin"
        }
        (p2_manifest / "plugin.json").write_text(json.dumps(manifest2), encoding="utf-8")
        mcp2 = {
            "mcpServers": {
                "ext-server": {
                    "command": "python",
                    "args": ["start.py"]
                }
            }
        }
        (p2 / ".mcp.json").write_text(json.dumps(mcp2), encoding="utf-8")
        
        configs = find_plugin_mcp_configs(settings)
        # Should discover both
        assert len(configs) == 2
        
        # Verify inline-plugin config
        inline_conf = [c for c in configs if c[0].name == "inline-plugin"]
        assert len(inline_conf) == 1
        assert "inline-server" in inline_conf[0][1]
        
        # Verify ext-plugin config
        ext_conf = [c for c in configs if c[0].name == "ext-plugin"]
        assert len(ext_conf) == 1
        assert "ext-server" in ext_conf[0][1]


def test_mcp_manager_from_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        settings.project_state_dir.mkdir(parents=True, exist_ok=True)
        
        # Create main mcp.json
        main_mcp = {
            "mcpServers": {
                "main-server": {
                    "command": "echo",
                    "args": ["hello"]
                }
            }
        }
        settings.mcp_config.write_text(json.dumps(main_mcp), encoding="utf-8")
        
        # Create a plugin with inline mcpServers in plugin.json
        p1 = settings.plugins_dir / "inline-plugin"
        p1_manifest = p1 / ".claude-plugin"
        p1_manifest.mkdir(parents=True, exist_ok=True)
        manifest1 = {
            "name": "inline-plugin",
            "mcpServers": {
                "inline-server": {
                    "command": "node",
                    "args": ["${__dirname}/index.js"]
                }
            }
        }
        (p1_manifest / "plugin.json").write_text(json.dumps(manifest1), encoding="utf-8")
        
        manager = MCPManager.from_settings(settings)
        
        # Verify both main and plugin servers are loaded
        assert "main-server" in manager._servers
        assert "inline-server" in manager._servers
        
        # Verify path resolution
        inline_server = manager._servers["inline-server"]
        p1_resolved_path = str(p1.resolve()).replace("\\", "/")
        assert inline_server.args[0] == f"{p1_resolved_path}/index.js"
        assert inline_server.cwd == p1_resolved_path
        
        # Verify specs
        specs = _load_mcp_specs(settings)
        names = {s["name"] for s in specs}
        assert "mcp:main-server" in names
        assert "mcp:inline-server" in names
