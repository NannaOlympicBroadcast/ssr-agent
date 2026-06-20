from __future__ import annotations

import shutil
from pathlib import Path

def install_builtin_plugins(target_dir: Path, force: bool = False) -> list[str]:
    """Copy the bundled built-in plugins into ``target_dir`` (~/.ssr/plugins)."""
    src_root = Path(__file__).resolve().parent.parent / "builtin_plugins"
    target_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    if not src_root.exists():
        return installed
    for plugin_dir in sorted(src_root.iterdir()):
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
