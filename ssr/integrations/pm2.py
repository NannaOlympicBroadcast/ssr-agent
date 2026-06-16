"""Background agent tasks managed by pm2.

SSR can schedule recurring/background agent jobs through pm2. Each task is run as
``ssr task-run --task <name>`` and registered with pm2 (optionally on a cron
restart schedule). Requires the ``pm2`` CLI on PATH (``npm i -g pm2``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings


@dataclass
class BackgroundTask:
    name: str
    prompt: str
    cwd: str
    cron: str | None = None  # e.g. "*/30 * * * *"


def pm2_available() -> bool:
    return shutil.which("pm2") is not None


def tasks_file(settings: Settings) -> Path:
    return settings.home / "tasks.json"


def _load_tasks(settings: Settings) -> dict[str, dict]:
    f = tasks_file(settings)
    if f.exists():
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_tasks(settings: Settings, tasks: dict[str, dict]) -> None:
    tasks_file(settings).write_text(json.dumps(tasks, ensure_ascii=False, indent=2), "utf-8")


def create_task(settings: Settings, task: BackgroundTask) -> str:
    """Register a background agent task and start it under pm2."""
    tasks = _load_tasks(settings)
    tasks[task.name] = {"prompt": task.prompt, "cwd": task.cwd, "cron": task.cron}
    _save_tasks(settings, tasks)

    if not pm2_available():
        return (
            f"Task '{task.name}' saved to {tasks_file(settings)}. "
            "pm2 not found on PATH — install with `npm i -g pm2` to run it."
        )

    cmd = [
        "pm2", "start", "ssr",
        "--name", f"ssr-task-{task.name}",
        "--no-autorestart",
        "--",
        "task-run", "--task", task.name,
    ]
    if task.cron:
        cmd[3:3] = ["--cron-restart", task.cron]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return f"pm2 task 'ssr-task-{task.name}' started" + (f" (cron: {task.cron})" if task.cron else "")
    except subprocess.CalledProcessError as e:
        return f"ERROR starting pm2 task: {e.stderr or e}"


def list_tasks(settings: Settings) -> dict[str, dict]:
    return _load_tasks(settings)


def delete_task(settings: Settings, name: str) -> str:
    tasks = _load_tasks(settings)
    tasks.pop(name, None)
    _save_tasks(settings, tasks)
    if pm2_available():
        subprocess.run(["pm2", "delete", f"ssr-task-{name}"], capture_output=True, text=True)
    return f"Task '{name}' removed."


def run_task(settings: Settings, name: str) -> str:
    """Execute a stored task once (invoked by pm2 / cron)."""
    from ..agent.core import SSRAgent

    tasks = _load_tasks(settings)
    task = tasks.get(name)
    if not task:
        return f"ERROR: no such task '{name}'"
    settings.project_dir = Path(task.get("cwd", settings.project_dir)).expanduser()
    agent = SSRAgent(settings)
    return agent.run(task["prompt"])
