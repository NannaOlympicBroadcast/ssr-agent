"""Minimal Claude-Code-inspired hooks runner."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .config import Settings


def hook_files(settings: Settings) -> list[Path]:
    return [settings.home / "hooks.json", settings.project_state_dir / "hooks.json"]


def load_hooks(settings: Settings) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    for path in hook_files(settings):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError:
            continue
        hooks = data.get("hooks", data)
        if isinstance(hooks, dict):
            for event, entries in hooks.items():
                if isinstance(entries, dict):
                    entries = [entries]
                merged.setdefault(event, []).extend([e for e in entries if isinstance(e, dict)])
    return merged


def run_hooks(settings: Settings, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for h in load_hooks(settings).get(event, []):
        cmd = h.get("command")
        if not cmd:
            continue
        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload),
                text=True,
                shell=True,
                cwd=settings.project_dir,
                capture_output=True,
                timeout=int(h.get("timeout", 30)),
            )
            results.append({
                "event": event,
                "command": cmd,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            })
        except Exception as e:
            results.append({"event": event, "command": cmd, "error": str(e)})
    return results
