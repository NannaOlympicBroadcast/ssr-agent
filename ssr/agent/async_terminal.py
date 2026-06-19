"""Asynchronous background terminal session management."""

from __future__ import annotations

import uuid
import subprocess
import threading
from pathlib import Path
from ssr.agent.tools import decode_output

class TerminalSession:
    def __init__(self, command: str, cwd: Path | str, timeout: int, on_finished=None):
        self.id = uuid.uuid4().hex[:8]
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.on_finished = on_finished
        
        self.lock = threading.Lock()
        self.buffer = bytearray()
        self.max_buffer_size = 100 * 1024  # 100KB
        
        self.process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        
        self.thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.thread.start()

    def _reader_loop(self):
        try:
            while True:
                chunk = self.process.stdout.read1(4096)
                if not chunk:
                    break
                with self.lock:
                    self.buffer.extend(chunk)
                    if len(self.buffer) > self.max_buffer_size:
                        self.buffer = self.buffer[-self.max_buffer_size:]
        except Exception:
            pass
        finally:
            code = self.process.wait()
            if self.on_finished:
                try:
                    self.on_finished(self.id, code)
                except Exception:
                    pass

    def check_output(self) -> str:
        with self.lock:
            data = bytes(self.buffer)
            self.buffer.clear()
        return decode_output(data)

    def send_keys(self, keys: str) -> str:
        if self.process.poll() is not None:
            return "ERROR: process is no longer running"
        try:
            self.process.stdin.write(keys.encode("utf-8"))
            self.process.stdin.flush()
            return f"Keys sent to terminal {self.id}"
        except Exception as e:
            return f"ERROR sending keys: {e}"

    def send_input(self, text: str) -> str:
        return self.send_keys(text + "\n")

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def kill(self) -> str:
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.stdout.close()
        except Exception:
            pass
        return f"Terminal {self.id} killed"

class AsyncTerminal:
    def __init__(self):
        self.terminals: dict[str, TerminalSession] = {}

    def spawn(self, command: str, cwd: Path | str, timeout: int = 300, on_finished=None) -> str:
        session = TerminalSession(command, cwd, timeout, on_finished=on_finished)
        self.terminals[session.id] = session
        return session.id

    def check_output(self, terminal_id: str) -> str:
        session = self.terminals.get(terminal_id)
        if not session:
            return f"ERROR: terminal {terminal_id} not found"
        return session.check_output()

    def send_keys(self, terminal_id: str, keys: str) -> str:
        session = self.terminals.get(terminal_id)
        if not session:
            return f"ERROR: terminal {terminal_id} not found"
        return session.send_keys(keys)

    def send_input(self, terminal_id: str, text: str) -> str:
        session = self.terminals.get(terminal_id)
        if not session:
            return f"ERROR: terminal {terminal_id} not found"
        return session.send_input(text)

    def is_alive(self, terminal_id: str) -> bool:
        session = self.terminals.get(terminal_id)
        if not session:
            return False
        return session.is_alive()

    def kill(self, terminal_id: str) -> str:
        session = self.terminals.get(terminal_id)
        if not session:
            return f"ERROR: terminal {terminal_id} not found"
        res = session.kill()
        return res

    def list_terminals(self) -> list[dict]:
        return [
            {
                "id": tid,
                "command": t.command,
                "alive": t.is_alive(),
            }
            for tid, t in self.terminals.items()
        ]

    def cleanup(self) -> None:
        for t in list(self.terminals.values()):
            try:
                t.kill()
            except Exception:
                pass
        self.terminals.clear()
