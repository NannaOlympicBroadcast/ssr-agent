"""IM-specific slash command handler."""

from __future__ import annotations

import os
from pathlib import Path
from ssr.agent.core import SSRAgent
from ssr.config import Settings
from ssr.approval import get_active_approval, ApprovalDecision

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

    if cmd == "/project":
        if not args:
            reply(f"Current project directory: `{agent.settings.project_dir}`")
        else:
            path_str = " ".join(args)
            p = Path(path_str).expanduser().resolve()
            agent.settings.project_dir = p
            agent.toolkit.settings.project_dir = p
            reply(f"Switched project directory to: `{p}`")
            
    elif cmd == "/session":
        if not args:
            reply("Usage: /session <list|resume <id>|new>")
        elif args[0] == "list":
            sessions = agent.sessions.list()
            if not sessions:
                reply("No recorded sessions yet.")
            else:
                lines = ["Recorded sessions:"]
                for s in sessions[:20]:
                    marker = " ●" if s.get("id") == agent.session_id else ""
                    lines.append(f"- `{s.get('id')}`: {s.get('title') or '(untitled)'}{marker}")
                reply("\n".join(lines))
        elif args[0] == "resume" and len(args) > 1:
            sid = args[1]
            if agent.load_session(sid):
                reply(f"Resumed session `{sid}`.")
            else:
                reply(f"Session `{sid}` not found.")
        elif args[0] == "new":
            sid = agent.new_session()
            reply(f"Started new session `{sid}`.")
        else:
            reply("Usage: /session <list|resume <id>|new>")
            
    elif cmd == "/model":
        if not args:
            primary = agent.models_config.get_primary()
            reply(f"Current model: `{primary.id}` ({primary.provider}/{primary.model})")
        elif args[0] == "list":
            primary = agent.models_config.get_primary()
            lines = ["Configured models:"]
            for m in agent.models_config.list_models():
                status = "primary" if m.id == primary.id else "fallback"
                lines.append(f"- `{m.id}` ({m.provider}/{m.model}) [{status}]")
            reply("\n".join(lines))
        elif args[0] == "switch" and len(args) > 1:
            target_id = args[1]
            if agent.models_config.switch_primary(target_id):
                reply(f"Switched primary model to: `{target_id}`")
            else:
                reply(f"Model `{target_id}` not found.")
        else:
            reply("Usage: /model [list|switch <id>]")
            
    elif cmd == "/bypass-permissions":
        pm = agent.toolkit.permission_manager
        pm.turbo_mode = not pm.turbo_mode
        reply(f"Turbo mode (bypass permissions): `{pm.turbo_mode}`")
        
    elif cmd in ("/approve", "/alwaysallow", "/disallow"):
        approval = get_active_approval()
        if not approval:
            reply("No pending command requires approval.")
        else:
            if cmd == "/approve":
                approval.decision = ApprovalDecision.ALLOW_ONCE
                approval.event.set()
                reply("Command approved (once).")
            elif cmd == "/alwaysallow":
                approval.decision = ApprovalDecision.ALWAYS_ALLOW
                approval.event.set()
                reply("Command pattern always allowed.")
            elif cmd == "/disallow":
                reason = " ".join(args) if args else "Denied by user"
                approval.decision = ApprovalDecision.DENY
                approval.reason = reason
                approval.event.set()
                reply(f"Command disallowed: {reason}")
                
    elif cmd == "/goal":
        if not args:
            reply("Usage: /goal <goal description>")
        else:
            desc = " ".join(args)
            if hasattr(agent, "run_goal"):
                def run_goal_thread():
                    try:
                        reply(f"Starting goal: {desc}")
                        res = agent.run_goal(desc)
                        reply(f"Goal complete: {res}")
                    except Exception as e:
                        reply(f"Goal failed: {e}")
                import threading
                threading.Thread(target=run_goal_thread, daemon=True).start()
            else:
                reply("Goal mechanism is not loaded yet in this agent version.")
            
    else:
        reply(f"Unknown slash command: {cmd}")
        
    return True
