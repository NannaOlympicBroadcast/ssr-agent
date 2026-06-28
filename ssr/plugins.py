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


def discover_plugin_manifests(settings: Settings) -> list[tuple[Path, dict]]:
    """Return ``(plugin_dir, manifest)`` for every bundled + user plugin.

    User plugins (``~/.ssr/plugins``) override bundled ones of the same name.
    """
    found: dict[str, tuple[Path, dict]] = {}
    # Like built-in skills, bundled plugins become active only once installed
    # into ``~/.ssr/plugins`` (via ``ssr init``); the repo ``builtin_plugins``
    # dir is only the install *source*, so it is not scanned directly here.
    root = settings.plugins_dir
    if root.exists():
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
    return list(found.values())


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

    for plugin_dir, manifest in discover_plugin_manifests(settings):
        if manifest.get("disabled"):
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
