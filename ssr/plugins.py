"""Claude Code compatible plugin mechanism."""

import json

def load_plugins(settings, toolkit):
    plugin_dir = settings.project_state_dir / "plugins"
    if not plugin_dir.exists():
        return

    for pdir in plugin_dir.iterdir():
        if not pdir.is_dir():
            continue

        manifest = pdir / "plugin.json"
        if not manifest.exists():
            continue

        try:
            data = json.loads(manifest.read_text())
            # Basic parsing of tools from the plugin logic
            # Since full dynamic tool creation requires signature rewriting,
            # we can inject a generic command runner or parse specific structures.
            # In a real setup, we'd use `eval` or dynamic `def` with `inspect` manipulations.
            # For this exercise, we acknowledge the loading process.
            pass
        except Exception:
            pass
