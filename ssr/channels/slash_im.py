"""IM-specific slash command handler.

Slash commands in IM/channel mode are kept *in sync* with the command-line
(REPL) mode: anything the CLI ``/`` handler understands is available here too.
A handful of commands need IM-specific behaviour (asynchronous execution,
out-of-band approvals, interruption), so those are special-cased; everything
else is delegated to :func:`ssr.slash.handle` with a recording console whose
output is sent back to the user as a chat reply.
"""

from __future__ import annotations

import io
import threading

from ssr.agent.core import SSRAgent
from ssr.approval import get_active_approval, ApprovalDecision
from ssr.config import Settings

# Commands that must run with IM-specific semantics rather than the synchronous
# CLI handler (they either block for a long time or coordinate with a running
# turn / pending approval).
_IM_SPECIAL = {
    "/approve", "/alwaysallow", "/disallow",
    "/stop", "/cancel", "/btw", "/goal",
}


def _delegate_to_cli(command_str: str, agent: SSRAgent, settings: Settings, reply) -> None:
    """Run a CLI slash command and forward its rendered output to the chat."""
    from rich.console import Console
    from ssr.slash import handle as cli_handle

    buf = io.StringIO()
    rec = Console(file=buf, width=100, no_color=True, highlight=False, emoji=False)
    try:
        cli_handle(command_str, agent, settings, rec)
    except Exception as e:  # never let a command crash the channel loop
        reply(f"Command error: {e}")
        return
    out = buf.getvalue().strip()
    reply(out or "(done)")


def handle_slash_im(
    command_str: str,
    agent: SSRAgent,
    settings: Settings,
    target: str,
    send_reply_fn,
) -> bool:
    """Handle a slash command in an IM context.

    Returns True if it was handled as a slash command, False otherwise.
    """
    parts = command_str.strip().split()
    if not parts or not parts[0].startswith("/"):
        return False

    cmd = parts[0].lower()
    args = parts[1:]

    def reply(msg: str) -> None:
        send_reply_fn(target, msg)

    # --- interruption: stop the running main turn ------------------------
    if cmd in ("/stop", "/cancel"):
        if agent.request_stop():
            reply("⏹ Stop requested — the running task will halt shortly.")
        else:
            reply("Nothing is running.")

    # --- side question answered alongside a running main turn ------------
    elif cmd == "/btw":
        if not args:
            reply("Usage: /btw <question>")
        else:
            question = " ".join(args)

            def run_btw() -> None:
                try:
                    reply(f"💬 {agent.answer_side_question(question)}")
                except Exception as e:
                    reply(f"btw failed: {e}")

            threading.Thread(target=run_btw, daemon=True).start()

    # --- out-of-band command approval ------------------------------------
    elif cmd in ("/approve", "/alwaysallow", "/disallow"):
        approval = get_active_approval()
        if not approval:
            reply("No pending command requires approval.")
        elif cmd == "/approve":
            approval.decision = ApprovalDecision.ALLOW_ONCE
            approval.event.set()
            reply("Command approved (once).")
        elif cmd == "/alwaysallow":
            approval.decision = ApprovalDecision.ALWAYS_ALLOW
            approval.event.set()
            reply("Command pattern always allowed.")
        else:  # /disallow [reason]
            reason = " ".join(args) if args else "Denied by user"
            approval.decision = ApprovalDecision.DENY
            approval.reason = reason
            approval.event.set()
            reply(f"Command disallowed: {reason}")

    # --- long-running goal loop (kept asynchronous) ----------------------
    elif cmd == "/goal":
        if not args:
            reply("Usage: /goal <goal description>")
        else:
            desc = " ".join(args)

            def run_goal_thread() -> None:
                try:
                    reply(f"Starting goal: {desc}")
                    reply(f"Goal complete: {agent.run_goal(desc)}")
                except Exception as e:
                    reply(f"Goal failed: {e}")

            threading.Thread(target=run_goal_thread, daemon=True).start()

    # --- everything else: identical to the CLI command-line mode ---------
    else:
        _delegate_to_cli(command_str, agent, settings, reply)

    return True
