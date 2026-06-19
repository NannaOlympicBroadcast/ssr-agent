"""Command permission checking and allowed command patterns."""

from __future__ import annotations

import enum
import fnmatch
from pathlib import Path
from ssr.config import Settings

class PermissionResult(enum.Enum):
    ALLOWED = "ALLOWED"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    DENIED = "DENIED"

class PermissionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.home / "allowed_commands.txt"
        self._turbo_mode = False
        self.allowed_patterns = []
        self.load()

    @property
    def turbo_mode(self) -> bool:
        return self._turbo_mode

    @turbo_mode.setter
    def turbo_mode(self, val: bool) -> None:
        self._turbo_mode = val

    def load(self) -> None:
        self.allowed_patterns = []
        if self.path.exists():
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
                self.allowed_patterns = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
            except Exception:
                pass
        
        safe_defaults = [
            "ls", "ls *",
            "dir", "dir *",
            "cat *", "type *",
            "echo *",
            "pwd",
            "cd", "cd *",
            "git status", "git log", "git log *", "git diff", "git diff *", "git branch",
            "python -m pytest", "python -m pytest *",
            "pytest", "pytest *",
        ]
        for p in safe_defaults:
            if p not in self.allowed_patterns:
                self.allowed_patterns.append(p)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(self.allowed_patterns), encoding="utf-8")
        except Exception:
            pass

    def add_always_allow(self, pattern: str) -> None:
        pattern = pattern.strip()
        if pattern not in self.allowed_patterns:
            self.allowed_patterns.append(pattern)
            self.save()

    def check_permission(self, command: str) -> PermissionResult:
        if self.turbo_mode:
            return PermissionResult.ALLOWED
            
        cmd_str = command.strip()
        for pattern in self.allowed_patterns:
            if fnmatch.fnmatch(cmd_str, pattern) or fnmatch.fnmatch(cmd_str.split()[0] if cmd_str.split() else "", pattern):
                return PermissionResult.ALLOWED
                
        return PermissionResult.NEEDS_APPROVAL
