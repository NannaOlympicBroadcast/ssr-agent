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
    "/skills": "List discovered skills",
    "/memory": "Show MEMORY (project + global)",
    "/plan": "Show the agent's current plan",
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
        console.print("[green]Session cleared.[/green]")

    else:
        console.print(f"[red]Unknown command:[/red] {cmd}. Try /help")

    return True


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
