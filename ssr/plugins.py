"""Bundled and user *plugins* for SSR Agent.

A *plugin* packages one or more MCP servers (and, optionally, shared
credentials). The on-disk layout follows the Claude Code plugin convention so an
upstream plugin can be dropped in unchanged:

    <plugin>/
      .claude-plugin/plugin.json   # declaration: name / version / description
      .mcp.json                    # {"mcpServers": { ... }}

The MCP servers may also be declared inline as an ``mcpServers`` key inside
``plugin.json`` (the Chrome DevTools MCP style); a flat ``plugin.json`` /
``ssr.plugin.json`` at the plugin root is accepted too, for convenience.

Bundled plugins live in ``ssr/builtin_plugins/<name>/`` and are copied into
``~/.ssr/plugins`` on ``ssr init`` (like built-in skills). User plugins live in
``~/.ssr/plugins/<name>/``.

Credential sharing
------------------
A plugin may declare ``"credentials": "<namespace>"`` in its ``plugin.json``. Any
``${<namespace>.key}`` placeholder appearing in its server ``command``/``args``/
``env`` is filled from the shared JSON credential file ``~/.ssr/<namespace>.json``.
For example a plugin may reference ``${xiaomi.account}`` to reuse the ``xiaomi``
channel's credentials in ``~/.ssr/xiaomi.json``. (The former bundled ``miot``
plugin used this; Mi Home control now lives in the native Miloco integration —
see :mod:`ssr.integrations.miloco`.)
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .config import Settings

_BUILTIN_DIR = Path(__file__).resolve().parent / "builtin_plugins"
# Manifest (plugin declaration) lookup order: Claude Code's .claude-plugin first.
_MANIFEST_CANDIDATES = (
    Path(".claude-plugin") / "plugin.json",
    Path("plugin.json"),
    Path("ssr.plugin.json"),
)
_MCP_FILE = ".mcp.json"
_PLACEHOLDER = re.compile(r"\$\{([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\}")


def builtin_plugins_dir() -> Path:
    return _BUILTIN_DIR


def _manifest_in(directory: Path) -> Path | None:
    for rel in _MANIFEST_CANDIDATES:
        cand = directory / rel
        if cand.exists():
            return cand
    return None


def _plugin_servers(plugin_dir: Path, manifest: dict) -> dict[str, dict]:
    """Resolve a plugin's ``mcpServers`` from inline manifest or sibling .mcp.json."""
    inline = manifest.get("mcpServers")
    if isinstance(inline, dict):
        return inline
    mcp_path = plugin_dir / _MCP_FILE
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        servers = data.get("mcpServers", data)
        return servers if isinstance(servers, dict) else {}
    return {}


def load_credentials(settings: Settings, namespace: str) -> dict:
    """Load the shared credential JSON for ``namespace`` (``~/.ssr/<ns>.json``)."""
    path = settings.home / f"{namespace}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _resolve_placeholders(value, creds_for):
    """Recursively substitute ``${namespace.key}`` placeholders in str/list/dict.

    ``creds_for(namespace)`` returns the credential dict for a namespace (loaded
    lazily from ``~/.ssr/<namespace>.json``).
    """
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            ns, key = m.group(1), m.group(2)
            return str(creds_for(ns).get(key, ""))
        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, list):
        return [_resolve_placeholders(v, creds_for) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v, creds_for) for k, v in value.items()}
    return value


def _iter_plugin_dirs(settings: Settings, include_builtin: bool) -> dict[str, tuple[Path, dict]]:
    """Map plugin name -> ``(dir, manifest)``. When ``include_builtin`` is True the
    bundled ``builtin_plugins`` dir is scanned first, so an installed/user copy in
    ``~/.ssr/plugins`` of the same name overrides it."""
    found: dict[str, tuple[Path, dict]] = {}
    roots: list[Path] = []
    if include_builtin and _BUILTIN_DIR.exists():
        roots.append(_BUILTIN_DIR)
    if settings.plugins_dir.exists():
        roots.append(settings.plugins_dir)
    for root in roots:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = _manifest_in(entry)
            if manifest_path is None:
                continue
            try:
                manifest = json.loads(manifest_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = manifest.get("name") or entry.name
            found[name] = (entry, manifest)
    return found


def discover_plugin_manifests(settings: Settings) -> list[tuple[Path, dict]]:
    """Return ``(plugin_dir, manifest)`` for every installed user plugin.

    Bundled plugins that contribute MCP servers become active only once installed
    into ``~/.ssr/plugins`` (via ``ssr init``); the repo ``builtin_plugins`` dir is
    the install *source*, so it is not scanned here. (In-process plugin features —
    agent tools and bus handlers — DO read the builtin dir, see
    :func:`enabled_plugin_manifests`, so they work without a copy.)
    """
    return list(_iter_plugin_dirs(settings, include_builtin=False).values())


# --------------------------------------------------------------- plugin config
def plugin_config_path(settings: Settings) -> Path:
    return settings.home / "plugins.json"


def load_plugin_config(settings: Settings) -> dict:
    """Load ``~/.ssr/plugins.json`` (``{"disabled": [name, ...]}``)."""
    path = plugin_config_path(settings)
    if not path.exists():
        return {"disabled": []}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"disabled": []}
    if not isinstance(data, dict):
        return {"disabled": []}
    data.setdefault("disabled", [])
    return data


def save_plugin_config(settings: Settings, cfg: dict) -> None:
    plugin_config_path(settings).write_text(json.dumps(cfg, indent=2) + "\n", "utf-8")


def set_plugin_enabled(settings: Settings, name: str, enabled: bool) -> dict:
    """Enable/disable a plugin in ``~/.ssr/plugins.json``; returns the new config."""
    cfg = load_plugin_config(settings)
    disabled = set(cfg.get("disabled", []))
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    cfg["disabled"] = sorted(disabled)
    save_plugin_config(settings, cfg)
    return cfg


def _is_enabled(name: str, manifest: dict, cfg: dict) -> bool:
    if manifest.get("disabled"):
        return False
    return name not in set(cfg.get("disabled", []))


def enabled_plugin_manifests(settings: Settings,
                             include_builtin: bool = True) -> list[tuple[str, Path, dict]]:
    """``(name, dir, manifest)`` for every enabled plugin (manifest ``disabled`` and
    ``plugins.json`` both respected)."""
    cfg = load_plugin_config(settings)
    out = []
    for name, (plugin_dir, manifest) in _iter_plugin_dirs(settings, include_builtin).items():
        if _is_enabled(name, manifest, cfg):
            out.append((name, plugin_dir, manifest))
    return out


def list_plugins(settings: Settings, include_builtin: bool = True) -> list[dict]:
    """Describe every discoverable plugin (for ``ssr plugin list``)."""
    cfg = load_plugin_config(settings)
    res = []
    for name, (plugin_dir, manifest) in sorted(_iter_plugin_dirs(settings, include_builtin).items()):
        res.append({
            "name": name,
            "enabled": _is_enabled(name, manifest, cfg),
            "version": manifest.get("version", ""),
            "description": manifest.get("description", ""),
            "mcp_servers": list(_plugin_servers(plugin_dir, manifest).keys()),
            "agent_tools": manifest.get("agent_tools") or manifest.get("agentTools") or [],
            "handlers": manifest.get("handlers") or manifest.get("busHandlers") or [],
            "dir": str(plugin_dir),
        })
    return res


def plugin_agent_tools(settings: Settings) -> list[str]:
    """``"module:attr"`` agent-tool specs declared by enabled plugins. Each attr is a
    class constructed with the ToolKit and exposing ``callables()``."""
    specs: list[str] = []
    for _name, _dir, manifest in enabled_plugin_manifests(settings):
        for spec in (manifest.get("agent_tools") or manifest.get("agentTools") or []):
            if isinstance(spec, str) and spec.strip():
                specs.append(spec.strip())
    return specs


def plugin_bus_handlers(settings: Settings) -> list[dict]:
    """Bus-handler declarations from enabled plugins. Each is a dict with at least
    ``event`` and a ``kind`` (subagent/mcp_tool/shell/python) + that kind's fields."""
    handlers: list[dict] = []
    for name, _dir, manifest in enabled_plugin_manifests(settings):
        for h in (manifest.get("handlers") or manifest.get("busHandlers") or []):
            if isinstance(h, dict) and h.get("event"):
                handlers.append({**h, "_plugin": name})
    return handlers


def plugin_mcp_servers(settings: Settings) -> dict[str, dict]:
    """Merged ``mcpServers`` contributed by all enabled plugins.

    Credential placeholders are resolved from each plugin's shared namespace.
    """
    servers: dict[str, dict] = {}
    creds_cache: dict[str, dict] = {}

    def creds_for(ns: str) -> dict:
        if ns not in creds_cache:
            creds_cache[ns] = load_credentials(settings, ns)
        return creds_cache[ns]

    cfg = load_plugin_config(settings)
    for plugin_dir, manifest in discover_plugin_manifests(settings):
        name = manifest.get("name") or plugin_dir.name
        if not _is_enabled(name, manifest, cfg):
            continue
        # Eagerly load the declared credential namespace (if any); other
        # ``${ns.key}`` references are resolved lazily by namespace on demand.
        declared = manifest.get("credentials")
        if declared:
            creds_for(declared)
        for srv_name, srv_cfg in _plugin_servers(plugin_dir, manifest).items():
            servers[srv_name] = _resolve_placeholders(srv_cfg, creds_for)
    return servers


def merged_mcp_servers(settings: Settings) -> dict[str, dict]:
    """Combine ``~/.ssr/mcp.json`` servers with plugin-contributed servers.

    Entries from ``mcp.json`` win on a name clash (user config is authoritative).
    """
    from .integrations.mcp_client import _read_server_configs

    merged = dict(plugin_mcp_servers(settings))
    merged.update(_read_server_configs(settings.mcp_config))
    return merged


def install_builtin_plugins(target_dir: Path, force: bool = False) -> list[str]:
    """Copy bundled plugins into ``target_dir`` (``~/.ssr/plugins``)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    if not _BUILTIN_DIR.exists():
        return installed
    for plugin_dir in sorted(_BUILTIN_DIR.iterdir()):
        if not plugin_dir.is_dir():
            continue
        dest = target_dir / plugin_dir.name
        if dest.exists() and not force:
            continue
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(plugin_dir, dest)
        installed.append(plugin_dir.name)
    return installed
