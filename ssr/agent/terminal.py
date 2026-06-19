"""Async terminal task manager for agent command execution."""
from __future__ import annotations

import subprocess, time, uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock


def decode_output(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")

@dataclass
class TerminalTask:
    id: str
    process: subprocess.Popen
    created: float = field(default_factory=time.time)
    offset: int = 0

class TerminalManager:
    def __init__(self):
        self._tasks: dict[str, TerminalTask] = {}
        self._lock = Lock()

    def start(self, command: str, cwd: Path) -> str:
        tid = uuid.uuid4().hex[:12]
        proc = subprocess.Popen(command, shell=True, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with self._lock:
            self._tasks[tid] = TerminalTask(tid, proc)
        return tid

    def read(self, task_id: str) -> str:
        task = self._tasks.get(task_id)
        if not task:
            return f"ERROR: unknown terminal task {task_id}"
        proc = task.process
        data = b""
        if proc.stdout:
            import fcntl, os
            fd = proc.stdout.fileno(); old = fcntl.fcntl(fd, fcntl.F_GETFL); fcntl.fcntl(fd, fcntl.F_SETFL, old | os.O_NONBLOCK)
            try:
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk: break
                    data += chunk
            except Exception:
                pass
        status = "running" if proc.poll() is None else f"exit={proc.returncode}"
        return f"task={task_id} status={status}\n{decode_output(data)}".strip()

    def send(self, task_id: str, text: str) -> str:
        task = self._tasks.get(task_id)
        if not task or not task.process.stdin:
            return f"ERROR: unknown terminal task {task_id}"
        task.process.stdin.write(text.encode()); task.process.stdin.flush()
        return f"sent {len(text)} bytes to {task_id}"

TERMINALS = TerminalManager()
