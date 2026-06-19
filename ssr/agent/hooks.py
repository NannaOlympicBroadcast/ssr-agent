"""Hook execution mechanism."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from ..config import Settings

def load_hooks(settings: Settings) -> list[dict]:
    hooks = []

    global_hooks = settings.home / "hooks.json"
    if global_hooks.exists():
        try:
            data = json.loads(global_hooks.read_text())
            if isinstance(data.get("hooks"), dict):
                hooks.append(data["hooks"])
        except Exception:
            pass

    proj_hooks = settings.project_state_dir / "hooks.json"
    if proj_hooks.exists():
        try:
            data = json.loads(proj_hooks.read_text())
            if isinstance(data.get("hooks"), dict):
                hooks.append(data["hooks"])
        except Exception:
            pass

    return hooks

def run_hooks(settings: Settings, event_type: str, context: dict[str, Any] = None) -> None:
    if context is None:
        context = {}

    all_hooks = load_hooks(settings)

    for hook_config in all_hooks:
        if event_type in hook_config:
            handlers = hook_config[event_type]
            if not isinstance(handlers, list):
                continue

            for handler in handlers:
                # Basic matcher logic: if matcher is provided, check if it matches tool name
                matcher = handler.get("matcher")
                if matcher and "tool_name" in context:
                    try:
                        if not re.search(matcher, context["tool_name"]):
                            continue
                    except re.error:
                        continue

                actions = handler.get("hooks", [])
                for action in actions:
                    if action.get("type") == "command":
                        cmd = action.get("command")
                        if cmd:
                            try:
                                # Replace variables like ${CLAUDE_PLUGIN_ROOT} -> settings.home
                                cmd = cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(settings.home))
                                subprocess.run(
                                    cmd,
                                    shell=True,
                                    cwd=str(settings.project_dir),
                                    capture_output=True
                                )
                            except Exception:
                                pass
