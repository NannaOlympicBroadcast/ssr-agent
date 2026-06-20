"""Slash-command handling for the interactive REPL."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from .agent.core import SSRAgent
from .config import Settings

HELP = {
    "/help": "Show this help",
    "/index": "(Re)build the model2vec context index. Usage: /index [category]",
    "/status": "Show index status and context-pool summary",
    "/context": "Search the context pool. Usage: /context <mode> <query>",
    "/attach": "Send an image/audio file: /attach <path> [prompt]  (aliases /image /audio)",
    "/skills": "List discovered skills",
    "/memory": "Show MEMORY (project + global)",
    "/plan": "Show the agent's current plan",
    "/project": "Show or switch project directory: /project [path]",
    "/model": "Manage models: /model [list|switch <id>]",
    "/session": "Manage sessions: /session <list|resume <id>|new>",
    "/goal": "Run until a goal is satisfied: /goal <goal description>",
    "/bypass-permissions": "Bypass command approval (toggle turbo mode)",
    "/stop": "Interrupt the running task (also typeable while the agent runs)",
    "/btw": "Ask a side question while the main task keeps running: /btw <question>",
    "/approve": "Approve the pending command",
    "/alwaysallow": "Always allow the pending command pattern",
    "/disallow": "Deny the pending command: /disallow [reason]",
    "/sessions": "List recorded conversation sessions (alias for /session list)",
    "/clear": "Reset the conversation session",
    "/quit": "Exit SSR (aliases: /exit, /q)",
}


def handle(command: str, agent: SSRAgent, settings: Settings, console: Console) -> bool:
    """Handle a slash command. Returns False if the REPL should exit."""
    parts = command.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/quit", "/exit", "/q"):
        return False

    if cmd == "/help":
        table = Table(title="SSR slash commands", show_header=True, header_style="bold cyan")
        table.add_column("Command")
        table.add_column("Description")
        for k, v in HELP.items():
            table.add_row(k, v)
        console.print(table)

    elif cmd == "/index":
        cats = [args[0]] if args else None
        with console.status("[cyan]Building model2vec index…"):
            counts = agent.retriever.reindex(cats)
        console.print("[green]Indexed:[/green] " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    elif cmd == "/status":
        _print_status(agent, console)

    elif cmd == "/context":
        if len(args) < 2:
            console.print("[yellow]Usage: /context <classic|embedding> <query>[/yellow]")
        else:
            mode, query = args[0], " ".join(args[1:])
            results = agent.retriever.search(query, mode=mode, top_k=6)
            if not results:
                console.print("[dim](no matching context)[/dim]")
            for r in results:
                console.print(r.render())

    elif cmd in ("/attach", "/image", "/audio"):
        _handle_attach(cmd, args, agent, console)

    elif cmd == "/skills":
        skills = agent.retriever.pool().by_category("skills")
        if not skills:
            console.print("[dim]No skills found.[/dim]")
        for s in skills:
            console.print(f"[bold]{s.title}[/bold] — {s.metadata.get('description', '')}\n  {s.source}")

    elif cmd == "/memory":
        console.print("[bold cyan]Project memory[/bold cyan]")
        console.print(agent.memory.recall("project") or "[dim](empty)[/dim]")
        console.print("[bold cyan]Global memory[/bold cyan]")
        console.print(agent.memory.recall("global") or "[dim](empty)[/dim]")

    elif cmd == "/plan":
        console.print(agent.toolkit.get_plan())

    elif cmd == "/project":
        if not args:
            console.print(f"Current project directory: [bold cyan]{agent.settings.project_dir}[/bold cyan]")
        else:
            p = Path(" ".join(args)).expanduser().resolve()
            agent.settings.project_dir = p
            agent.toolkit.settings.project_dir = p
            settings.project_dir = p
            console.print(f"[green]Switched project directory to:[/green] [bold cyan]{p}[/bold cyan]")

    elif cmd == "/clear":
        agent._history.clear()
        agent.session_id = None  # next turn starts a fresh recorded session
        console.print("[green]Session cleared.[/green]")

    elif cmd in ("/session", "/sessions"):
        if cmd == "/sessions" or (args and args[0] == "list"):
            _print_sessions(agent, console)
        elif not args:
            console.print("[yellow]Usage: /session <list|resume <id>|new>[/yellow]")
        elif args[0] == "resume" and len(args) > 1:
            sid = args[1]
            if agent.load_session(sid):
                console.print(f"[green]Resumed session[/green] [bold cyan]{sid}[/bold cyan].")
            else:
                console.print(f"[red]Session '{sid}' not found.[/red]")
        elif args[0] == "new":
            sid = agent.new_session()
            console.print(f"[green]Started new session[/green] [bold cyan]{sid}[/bold cyan].")
        else:
            console.print("[yellow]Usage: /session <list|resume <id>|new>[/yellow]")

    elif cmd == "/goal":
        if not args:
            console.print("[yellow]Usage: /goal <goal description>[/yellow]")
        else:
            goal = " ".join(args)
            console.print(f"[dim magenta]working toward goal…[/dim magenta] {goal}")
            from .cli import _run_turn_with_controls
            def turn_runner(prompt: str) -> str:
                return _run_turn_with_controls(agent, prompt, console)
            console.print(agent.run_goal(goal, turn_runner=turn_runner))

    elif cmd == "/model":
        if not args:
            primary = agent.models_config.get_primary()
            console.print(f"Current model: [bold cyan]{primary.id}[/bold cyan] ({primary.provider}/{primary.model})")
        elif args[0] == "list":
            table = Table(title="Configured models", header_style="bold cyan")
            table.add_column("ID")
            table.add_column("Provider")
            table.add_column("Model")
            table.add_column("API Key Env")
            table.add_column("Base URL")
            table.add_column("Status")
            
            primary = agent.models_config.get_primary()
            for m in agent.models_config.list_models():
                status = "[green]primary[/green]" if m.id == primary.id else "fallback"
                table.add_row(
                    m.id,
                    m.provider,
                    m.model,
                    m.api_key_env,
                    m.base_url or "—",
                    status
                )
            console.print(table)
        elif args[0] == "switch" and len(args) > 1:
            target_id = args[1]
            if agent.models_config.switch_primary(target_id):
                console.print(f"[green]Switched primary model to {target_id}[/green]")
            else:
                console.print(f"[red]Model '{target_id}' not found in configuration.[/red]")
        else:
            console.print("[yellow]Usage: /model [list|switch <id>][/yellow]")

    elif cmd in ("/stop", "/cancel"):
        if agent.request_stop():
            console.print("[yellow]⏹ stop requested.[/yellow]")
        else:
            console.print("[dim]Nothing is running.[/dim]")

    elif cmd == "/btw":
        if not args:
            console.print("[yellow]Usage: /btw <question>[/yellow]")
        else:
            console.print(agent.answer_side_question(" ".join(args)))

    elif cmd == "/bypass-permissions":
        pm = agent.toolkit.permission_manager
        pm.turbo_mode = not pm.turbo_mode
        console.print(f"Turbo mode (bypass permissions): [bold cyan]{pm.turbo_mode}[/bold cyan]")

    elif cmd in ("/approve", "/alwaysallow", "/disallow"):
        from ssr.approval import get_active_approval, ApprovalDecision
        approval = get_active_approval()
        if not approval:
            console.print("[yellow]No pending command requires approval.[/yellow]")
        else:
            if cmd == "/approve":
                approval.decision = ApprovalDecision.ALLOW_ONCE
                approval.event.set()
                console.print("[green]Command approved (once).[/green]")
            elif cmd == "/alwaysallow":
                approval.decision = ApprovalDecision.ALWAYS_ALLOW
                approval.event.set()
                console.print("[green]Command pattern always allowed.[/green]")
            elif cmd == "/disallow":
                reason = " ".join(args) if args else "Denied by user"
                approval.decision = ApprovalDecision.DENY
                approval.reason = reason
                approval.event.set()
                console.print(f"[red]Command disallowed: {reason}[/red]")

    else:
        console.print(f"[red]Unknown command:[/red] {cmd}. Try /help")

    return True


def _handle_attach(cmd: str, args: list[str], agent: SSRAgent, console: Console) -> None:
    """`/attach <path> [prompt]` — send an image or audio file to the agent."""
    import mimetypes
    from pathlib import Path

    if not args:
        console.print("[yellow]Usage: /attach <path> [prompt]   (aliases: /image, /audio)[/yellow]")
        return
    path = Path(args[0]).expanduser()
    prompt = " ".join(args[1:])
    if not path.exists():
        console.print(f"[red]No such file:[/red] {path}")
        return
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("image/"):
        kind = "image"
    elif mime and mime.startswith("audio/"):
        kind = "audio"
    else:  # fall back to the command name
        kind = "audio" if cmd == "/audio" else "image"
        mime = mime or ("audio/wav" if kind == "audio" else "image/png")
    data = path.read_bytes()
    parts: list[dict] = []
    if prompt:
        parts.append({"type": "text", "text": prompt})
    parts.append({"type": kind, "mime_type": mime, "data": data})
    console.print(
        f"[dim magenta]thinking… (attached {kind} {path.name}, {len(data)} bytes, {mime})[/dim magenta]"
    )
    reply = agent.run_parts(parts)
    console.print(f"\n[bold green]ssr ▸[/bold green] {reply}\n")


def _print_sessions(agent: SSRAgent, console: Console) -> None:
    sessions = agent.sessions.list()
    if not sessions:
        console.print("[dim]No recorded sessions yet.[/dim]")
        return
    table = Table(title="Recorded sessions", header_style="bold cyan")
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("Updated")
    table.add_column("Turns", justify="right")
    table.add_column("Title")
    for s in sessions[:30]:
        marker = " [green]●[/green]" if s.get("id") == agent.session_id else ""
        table.add_row(
            s.get("id") or "—",
            (s.get("updated") or s.get("created") or "")[:19].replace("T", " "),
            str(s.get("turns", 0)),
            (s.get("title") or "(untitled)") + marker,
        )
    console.print(table)
    console.print("[dim]Resume with /session resume <session-id>[/dim]")


def _print_status(agent: SSRAgent, console: Console) -> None:
    summary = agent.retriever.pool().categories_summary()
    table = Table(title="Context pool", header_style="bold magenta")
    table.add_column("Category")
    table.add_column("Items", justify="right")
    table.add_column("Index", justify="right")
    status = agent.retriever.index_status()
    for cat, n in summary.items():
        idx = status.get(cat, {})
        backend = idx.get("backend", "—")
        indexed = idx.get("items", 0)
        table.add_row(cat, str(n), f"{indexed} ({backend})")
    console.print(table)
