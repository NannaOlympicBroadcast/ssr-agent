"""Approval handlers for user confirmation of command execution."""

from __future__ import annotations

import abc
import enum
import threading
import time
from rich.console import Console

class ApprovalDecision(enum.Enum):
    ALLOW_ONCE = "ALLOW_ONCE"
    ALWAYS_ALLOW = "ALWAYS_ALLOW"
    DENY = "DENY"

class ApprovalHandler(abc.ABC):
    #: Reason the user gave for the most recent denial (empty when none / approved).
    #: ``run_command`` reads this back so the agent can adapt instead of retrying.
    denial_reason: str = ""

    @abc.abstractmethod
    def request_approval(self, command: str, context: dict | None = None) -> ApprovalDecision:
        """Prompt/Request user approval for a command."""
        pass

# Global state for handling out-of-band approvals (e.g. via IM or remote chat)
class PendingApproval:
    def __init__(self, command: str):
        self.command = command
        self.event = threading.Event()
        self.decision: ApprovalDecision = ApprovalDecision.DENY
        self.reason: str = ""

_pending_approval_lock = threading.Lock()
active_pending_approval: PendingApproval | None = None

def get_active_approval() -> PendingApproval | None:
    with _pending_approval_lock:
        return active_pending_approval

def set_active_approval(approval: PendingApproval | None) -> None:
    global active_pending_approval
    with _pending_approval_lock:
        active_pending_approval = approval

class TUIApprovalHandler(ApprovalHandler):
    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def request_approval(self, command: str, context: dict | None = None) -> ApprovalDecision:
        self.denial_reason = ""
        self.console.print(f"\n[bold yellow]⚠ Command requires approval:[/bold yellow] [bold cyan]{command}[/bold cyan]")
        self.console.print("[1] Allow once  [2] Always allow  [3] Deny (you may add a reason)")

        while True:
            try:
                choice = input("Choice (1-3): ").strip()
                if choice == "1":
                    return ApprovalDecision.ALLOW_ONCE
                elif choice == "2":
                    return ApprovalDecision.ALWAYS_ALLOW
                elif choice == "3":
                    # Let the user steer the agent instead of just refusing: a
                    # typed reason is handed back to the model as guidance.
                    try:
                        reason = input("Reason / what to do instead (optional): ").strip()
                    except (KeyboardInterrupt, EOFError):
                        reason = ""
                    self.denial_reason = reason
                    return ApprovalDecision.DENY
                else:
                    self.console.print("[yellow]Invalid choice, please select 1, 2, or 3.[/yellow]")
            except (KeyboardInterrupt, EOFError):
                return ApprovalDecision.DENY

class IMApprovalHandler(ApprovalHandler):
    def __init__(self, send_fn, timeout: float = 60.0):
        self.send_fn = send_fn
        self.timeout = timeout

    def request_approval(self, command: str, context: dict | None = None) -> ApprovalDecision:
        # Format target for IM sending
        target = (context or {}).get("target")
        self.send_fn(
            target,
            f"⚠ Command requires approval: `{command}`\nUse /approve, /alwaysallow, or /disallow [reason]"
        )
        
        approval = PendingApproval(command)
        set_active_approval(approval)
        self.denial_reason = ""

        # Block until event is set or timeout
        signaled = approval.event.wait(timeout=self.timeout)
        set_active_approval(None)

        if signaled:
            if approval.decision == ApprovalDecision.DENY:
                self.denial_reason = approval.reason
            return approval.decision
        else:
            self.send_fn(target, "❌ Command approval request timed out. Denied.")
            return ApprovalDecision.DENY

class RCApprovalHandler(ApprovalHandler):
    def __init__(self, send_fn, timeout: float = 60.0):
        self.send_fn = send_fn
        self.timeout = timeout

    def request_approval(self, command: str, context: dict | None = None) -> ApprovalDecision:
        # Remote control approval behaves similarly to IM approval
        target = (context or {}).get("target")
        self.send_fn(
            target,
            f"⚠ Command requires approval: `{command}`\nUse /approve, /alwaysallow, or /disallow [reason]"
        )
        
        approval = PendingApproval(command)
        set_active_approval(approval)
        self.denial_reason = ""

        signaled = approval.event.wait(timeout=self.timeout)
        set_active_approval(None)

        if signaled:
            if approval.decision == ApprovalDecision.DENY:
                self.denial_reason = approval.reason
            return approval.decision
        else:
            return ApprovalDecision.DENY

class AutoApprovalHandler(ApprovalHandler):
    def request_approval(self, command: str, context: dict | None = None) -> ApprovalDecision:
        return ApprovalDecision.ALLOW_ONCE
