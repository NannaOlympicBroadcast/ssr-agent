"""Slash-command handling for the interactive REPL."""

from __future__ import annotations

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
    "/sessions": "List recorded conversation sessions",
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

    elif cmd == "/clear":
        agent._history.clear()
        agent.session_id = None  # next turn starts a fresh recorded session
        console.print("[green]Session cleared.[/green]")

    elif cmd == "/sessions":
        _print_sessions(agent, console)

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
    table.add_column("Updated")
    table.add_column("Turns", justify="right")
    table.add_column("Title")
    for s in sessions[:30]:
        marker = " [green]●[/green]" if s.get("id") == agent.session_id else ""
        table.add_row(
            (s.get("updated") or s.get("created") or "")[:19].replace("T", " "),
            str(s.get("turns", 0)),
            (s.get("title") or "(untitled)") + marker,
        )
    console.print(table)


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
