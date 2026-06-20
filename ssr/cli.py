"""SSR Agent command-line entrypoint and interactive TUI."""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.markup import escape

from . import __version__
from .banner import render_banner
from .config import Settings, load_settings, missing_required
from .skills.manager import install_builtin_skills
from .plugins.manager import install_builtin_plugins


def _short(val, limit: int = 120) -> str:
    """One-line, length-capped, markup-safe rendering of a tool arg/result."""
    s = str(val).replace("\n", " ⏎ ")
    s = s[:limit] + ("…" if len(s) > limit else "")
    return escape(s)


def make_event_renderer(console: Console):
    """Return an on_event callback that prints agent thinking + tool activity."""

    def render(ev: dict) -> None:
        typ = ev.get("type")
        pad = "    " if ev.get("agent") == "sub" else ""
        if typ == "thinking":
            console.print(f"{pad}[dim italic]💭 {_short(ev.get('text', ''), 400)}[/dim italic]")
        elif typ == "sub_agent":
            console.print(f"[magenta]🤖 sub-agent[/magenta] [dim]{_short(ev.get('task', ''), 120)}[/dim]")
        elif typ == "tool_call":
            args = ev.get("args") or {}
            argstr = ", ".join(f"{k}={_short(v, 60)}" for k, v in args.items())
            console.print(f"{pad}[yellow]🔧 {escape(str(ev.get('name', '')))}[/yellow][dim]({argstr})[/dim]")
        elif typ == "tool_result":
            res = ev.get("result", "")
            style = "red" if str(res).startswith("ERROR") else "green"
            console.print(f"{pad}[dim]   ↳[/dim] [{style}]{_short(res, 200)}[/{style}]")
        elif typ == "terminal_completed":
            tid = ev.get("terminal_id")
            code = ev.get("exit_code")
            console.print(f"\n[bold green]🔔 Background terminal {tid} completed with exit code {code}[/bold green]")

    return render

ENV_TEMPLATE = """\
# SSR Agent configuration (~/.ssr/.env)
# Required:
GEMINI_API_KEY=
TAVILY_API_KEY=
DEFAULT_MODEL=gemini-3.1-flash-lite
"""

MCP_TEMPLATE = """\
{
  "mcpServers": {
  }
}
"""


def _first_run_setup(settings: Settings, console: Console) -> None:
    """Create ~/.ssr scaffolding, .env template, mcp.json and built-in skills."""
    settings.ensure_dirs()
    if not settings.env_file.exists():
        settings.env_file.write_text(ENV_TEMPLATE, "utf-8")
    if not settings.mcp_config.exists():
        settings.mcp_config.write_text(MCP_TEMPLATE, "utf-8")
    installed = install_builtin_skills(settings.skills_dir)
    if installed:
        console.print(f"[green]Installed built-in skills:[/green] {', '.join(installed)}")
    installed_plugins = install_builtin_plugins(settings.plugins_dir)
    if installed_plugins:
        console.print(f"[green]Installed built-in plugins:[/green] {', '.join(installed_plugins)}")


def cmd_init(args, settings: Settings, console: Console) -> int:
    _first_run_setup(settings, console)
    install_builtin_skills(settings.skills_dir, force=True)
    install_builtin_plugins(settings.plugins_dir, force=True)
    console.print(f"[green]✓[/green] SSR home ready at [bold]{settings.home}[/bold]")
    console.print(f"  Edit [bold]{settings.env_file}[/bold] to add GEMINI_API_KEY / TAVILY_API_KEY")
    return 0


def cmd_ask(args, settings: Settings, console: Console) -> int:
    from .agent.core import SSRAgent

    if args.cwd:
        settings = load_settings(args.cwd)
    missing = missing_required(settings)
    if "GEMINI_API_KEY" in missing:
        console.print("[red]GEMINI_API_KEY not set — configure ~/.ssr/.env[/red]")
        return 1
    agent = SSRAgent(settings)
    reply = agent.run(args.prompt)
    console.print(reply)
    return 0


def cmd_index(args, settings: Settings, console: Console) -> int:
    from .context_pool.retrieval import Retriever

    retr = Retriever(settings)
    cats = [args.category] if args.category else None
    with console.status("[cyan]Building model2vec index…"):
        counts = retr.reindex(cats)
    console.print("[green]Indexed:[/green] " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def cmd_task(args, settings: Settings, console: Console) -> int:
    from .integrations import pm2

    if args.task_action == "create":
        task = pm2.BackgroundTask(name=args.name, prompt=args.prompt, cwd=args.cwd or str(settings.project_dir), cron=args.cron)
        console.print(pm2.create_task(settings, task))
    elif args.task_action == "list":
        for name, t in pm2.list_tasks(settings).items():
            console.print(f"[bold]{name}[/bold] (cron={t.get('cron')}) cwd={t.get('cwd')}\n  {t.get('prompt')}")
    elif args.task_action == "delete":
        console.print(pm2.delete_task(settings, args.name))
    return 0


def cmd_task_run(args, settings: Settings, console: Console) -> int:
    from .integrations import pm2

    print(pm2.run_task(settings, args.task))
    return 0


def cmd_feishu(args, settings: Settings, console: Console) -> int:
    # Deprecated alias, mapping to channel command
    console.print("[yellow]WARNING: ssr feishu is deprecated, use ssr channel instead[/yellow]")
    args.channel_action = "config" if args.feishu_action == "configure" else "on"
    args.channel_name = "feishu"
    return cmd_channel(args, settings, console)


def cmd_channel(args, settings: Settings, console: Console) -> int:
    import ssr.channels  # trigger registration
    from ssr.channels.registry import registry
    import time
    
    action = args.channel_action
    if action == "configure":
        action = "config"
    elif action == "serve":
        action = "on"
    
    if action == "config":
        channel_name = args.channel_name
        channel = registry.get(channel_name)
        if not channel:
            console.print(f"[red]Channel '{channel_name}' not found.[/red]")
            return 1
        channel.configure(settings)
        return 0
        
    elif action == "on":
        channel_name = args.channel_name
        if channel_name == "all":
            import threading
            threads = []
            for ch in registry.list_channels():
                try:
                    t = threading.Thread(target=ch.serve, args=(settings,), daemon=True)
                    t.start()
                    threads.append(t)
                    console.print(f"[green]Started channel '{ch.name}' listening.[/green]")
                except Exception as e:
                    console.print(f"[red]Failed to start channel '{ch.name}': {e}[/red]")
            
            if threads:
                try:
                    while True:
                        time.sleep(1)
                except (KeyboardInterrupt, SystemExit):
                    pass
            return 0
        else:
            channel = registry.get(channel_name)
            if not channel:
                console.print(f"[red]Channel '{channel_name}' not found.[/red]")
                return 1
            channel.serve(settings)
            return 0
            
    elif action == "list":
        console.print("Registered channels:")
        for ch in registry.list_channels():
            console.print(f"- [bold cyan]{ch.name}[/bold cyan]")
        return 0
        
    elif action == "status":
        for ch in registry.list_channels():
            has_config = False
            if ch.name == "feishu":
                from ssr.integrations.feishu import load_config
                has_config = load_config(settings) is not None
            elif ch.name == "wechat":
                has_config = (settings.home / "wechat.json").exists()
            status = "[green]configured[/green]" if has_config else "[yellow]not configured[/yellow]"
            console.print(f"Channel [bold cyan]{ch.name}[/bold cyan]: {status}")
        return 0
        
    return 0


def cmd_acp(settings: Settings) -> int:
    from .integrations.acp import run_acp

    run_acp(settings)
    return 0


def cmd_rc(args, settings: Settings, console: Console) -> int:
    from .integrations import remote

    action = getattr(args, "rc_action", None)
    if action == "configure":
        remote.configure_interactive(settings)
        return 0
    if action == "status":
        return remote.show_status(settings)
    if action == "tags":
        tags = [t for chunk in args.tags for t in chunk.split(",")]
        return remote.set_tags(settings, tags)
    # default: connect (configuring on first run)
    return remote.run_remote(settings, reconfigure=bool(getattr(args, "reconfigure", False)))


def cmd_models(args, settings: Settings, console: Console) -> int:
    from .models import ModelsConfig, ModelEntry

    cfg = ModelsConfig(settings)
    action = args.models_action
    if action in ("config", "configure"):
        while True:
            console.print("\n[bold cyan]=== SSR Models Configuration ===[/bold cyan]")
            from rich.table import Table
            table = Table(title="Configured Models")
            table.add_column("ID", style="cyan")
            table.add_column("Provider", style="green")
            table.add_column("Model Name", style="magenta")
            table.add_column("API Key Env", style="yellow")
            table.add_column("Base URL", style="blue")
            table.add_column("Type", style="bold red")

            for m in cfg.list_models():
                is_primary = "Primary" if m.id == cfg.primary else "Fallback"
                table.add_row(
                    m.id,
                    m.provider,
                    m.model,
                    m.api_key_env,
                    m.base_url or "-",
                    is_primary
                )
            console.print(table)

            console.print("1. Add/Edit a model configuration")
            console.print("2. Set the primary model")
            console.print("3. Delete a model configuration")
            console.print("4. Save and Exit")

            try:
                choice = input("Select an option (1-4): ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Exit without saving changes.[/yellow]")
                break

            if choice == "1":
                try:
                    model_id = input("Enter unique model ID: ").strip()
                    if not model_id:
                        console.print("[red]ID cannot be empty.[/red]")
                        continue
                    provider = input("Enter provider (gemini, anthropic, openai): ").strip().lower()
                    if provider not in ("gemini", "anthropic", "openai"):
                        console.print("[red]Invalid provider. Must be gemini, anthropic, or openai.[/red]")
                        continue
                    model_name = input("Enter model name (e.g. gemini-1.5-flash, gpt-4o): ").strip()
                    if not model_name:
                        console.print("[red]Model name cannot be empty.[/red]")
                        continue
                    api_key_env = input("Enter API key environment variable name (e.g. GEMINI_API_KEY): ").strip()
                    if not api_key_env:
                        console.print("[red]API key environment variable name cannot be empty.[/red]")
                        continue
                    base_url = input("Enter base URL (optional, press Enter to skip): ").strip()
                    base_url = base_url if base_url else None

                    entry = ModelEntry(
                        id=model_id,
                        provider=provider,
                        model=model_name,
                        api_key_env=api_key_env,
                        base_url=base_url
                    )
                    cfg.add_model(entry)
                    console.print(f"[green]Added/Updated model {model_id} successfully.[/green]")
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Operation cancelled.[/yellow]")
            elif choice == "2":
                try:
                    model_id = input("Enter the ID of the model to set as primary: ").strip()
                    if cfg.switch_primary(model_id):
                        console.print(f"[green]Model {model_id} is now the primary model.[/green]")
                    else:
                        console.print(f"[red]Model {model_id} not found.[/red]")
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Operation cancelled.[/yellow]")
            elif choice == "3":
                try:
                    model_id = input("Enter the ID of the model to delete: ").strip()
                    if any(m.id == model_id for m in cfg.list_models()):
                        cfg.remove_model(model_id)
                        console.print(f"[green]Model {model_id} deleted successfully.[/green]")
                    else:
                        console.print(f"[red]Model {model_id} not found.[/red]")
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Operation cancelled.[/yellow]")
            elif choice == "4":
                cfg.save()
                console.print(f"[green]Configuration saved to {cfg.path}[/green]")
                break
            else:
                console.print("[red]Invalid choice. Please enter 1-4.[/red]")
    return 0


def cmd_serve(args, settings: Settings, console: Console) -> int:
    from .models import ModelsConfig
    cfg = ModelsConfig(settings)
    primary = cfg.get_primary()

    import os
    if not os.environ.get(primary.api_key_env):
        console.print(f"[red]Error: Primary model '{primary.id}' requires the environment variable '{primary.api_key_env}', which is not set.[/red]")
        console.print(f"Please configure it in ~/.ssr/.env or export it.")
        return 1

    from .serve import run_server
    run_server(settings, host=args.host, port=args.port)
    return 0


def repl(settings: Settings, console: Console, turbo_mode: bool = False) -> int:
    from .agent.core import SSRAgent
    from .slash import handle as handle_slash

    render_banner(console)

    missing = missing_required(settings)
    if missing:
        console.print(f"[yellow]⚠ Missing config in {settings.env_file}: {', '.join(missing)}[/yellow]")
    console.print(
        f"[dim]home={settings.home}  project={settings.project_dir}  model={settings.default_model}[/dim]"
    )
    console.print("[dim]Type /help for commands, /quit to exit.[/dim]\n")

    agent = SSRAgent(settings, on_event=make_event_renderer(console))
    if turbo_mode:
        agent.toolkit.permission_manager.turbo_mode = True

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory

        session = PromptSession(history=FileHistory(str(settings.home / "history")))
        prompt_fn = lambda: session.prompt("ssr ❯ ")  # noqa: E731
    except Exception:
        prompt_fn = lambda: input("ssr ❯ ")  # noqa: E731

    while True:
        try:
            line = prompt_fn().strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return 0
        if not line:
            continue
        if line.startswith("/"):
            if not handle_slash(line, agent, settings, console):
                console.print("[dim]bye[/dim]")
                return 0
            continue
        if "GEMINI_API_KEY" in missing_required(settings):
            console.print("[red]GEMINI_API_KEY not set — edit ~/.ssr/.env[/red]")
            continue
        console.print("[dim magenta]thinking…[/dim magenta]")
        reply = agent.run(line)
        console.print(f"\n[bold green]ssr ▸[/bold green] {reply}\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssr", description="SSR Agent — CLI coding agent (Google ADK)")
    p.add_argument("--version", action="store_true", help="print version and exit")
    p.add_argument("--experimental-acp", action="store_true", help="run as an ACP server over stdio")
    p.add_argument("--turbo-mode", action="store_true", help="bypass command approval permissions")
    p.add_argument("-C", "--cwd", help="project working directory")

    sub = p.add_subparsers(dest="command")

    sub.add_parser("init", help="initialise ~/.ssr (env, mcp.json, built-in skills)")

    ask = sub.add_parser("ask", help="one-shot prompt")
    ask.add_argument("prompt")
    ask.add_argument("--cwd", help="project directory")

    idx = sub.add_parser("index", help="(re)build the model2vec context index")
    idx.add_argument("category", nargs="?", help="tools|configurations|skills|memory")

    task = sub.add_parser("task", help="manage pm2 background agent tasks")
    tsub = task.add_subparsers(dest="task_action", required=True)
    tc = tsub.add_parser("create")
    tc.add_argument("name")
    tc.add_argument("prompt")
    tc.add_argument("--cwd")
    tc.add_argument("--cron", help="cron schedule, e.g. '*/30 * * * *'")
    tsub.add_parser("list")
    td = tsub.add_parser("delete")
    td.add_argument("name")

    tr = sub.add_parser("task-run", help="run a stored task once (used by pm2)")
    tr.add_argument("--task", required=True)

    fei = sub.add_parser("feishu", help="configure / serve the Feishu (Lark) bot")
    fsub = fei.add_subparsers(dest="feishu_action", required=True)
    fsub.add_parser("configure")
    fsub.add_parser("serve", help="run the bot over a WebSocket long connection")

    chan = sub.add_parser("channel", help="configure / run messaging channels (Feishu, WeChat)")
    chsub = chan.add_subparsers(dest="channel_action", required=True)
    chcfg = chsub.add_parser("config", aliases=["configure"], help="configure a channel")
    chcfg.add_argument("channel_name", choices=["feishu", "wechat"])
    chon = chsub.add_parser("on", aliases=["serve"], help="start listening on channel(s)")
    chon.add_argument("channel_name", choices=["feishu", "wechat", "all"], nargs="?", default="all")
    chsub.add_parser("list", help="list registered channels")
    chsub.add_parser("status", help="show channels configuration status")

    rc = sub.add_parser("rc", help="remote control: connect this instance to a dispatch server")
    rc.add_argument("--reconfigure", action="store_true", help="re-run interactive setup")
    rcsub = rc.add_subparsers(dest="rc_action")
    rcsub.add_parser("configure", help="set dispatch endpoint, token, node name and tags")
    rcsub.add_parser("status", help="show the saved remote-control configuration")
    rctags = rcsub.add_parser("tags", help="set this node's tags (comma/space separated)")
    rctags.add_argument("tags", nargs="+", help="tags, e.g. prod gpu  or  prod,gpu")

    models = sub.add_parser("models", help="manage LLM models configuration")
    models_sub = models.add_subparsers(dest="models_action", required=True)
    models_sub.add_parser("config", aliases=["configure"], help="interactively configure models")

    serve = sub.add_parser("serve", help="start an OpenAI-compatible HTTP API server")
    serve.add_argument("--host", default="127.0.0.1", help="host to bind the server to")
    serve.add_argument("--port", type=int, default=8000, help="port to bind the server to")

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()

    if args.version:
        console.print(f"ssr-agent {__version__}")
        return 0

    settings = load_settings(args.cwd)

    if args.experimental_acp:
        # ACP speaks pure JSON-RPC 2.0 on stdout — keep stdout clean and route
        # all setup/diagnostic output to stderr so the protocol isn't corrupted.
        _first_run_setup(settings, Console(stderr=True))
        return cmd_acp(settings)

    _first_run_setup(settings, console)
    if args.command == "init":
        return cmd_init(args, settings, console)
    if args.command == "ask":
        return cmd_ask(args, settings, console)
    if args.command == "index":
        return cmd_index(args, settings, console)
    if args.command == "task":
        return cmd_task(args, settings, console)
    if args.command == "task-run":
        return cmd_task_run(args, settings, console)
    if args.command == "feishu":
        return cmd_feishu(args, settings, console)
    if args.command == "channel":
        return cmd_channel(args, settings, console)
    if args.command == "rc":
        return cmd_rc(args, settings, console)
    if args.command == "models":
        return cmd_models(args, settings, console)
    if args.command == "serve":
        return cmd_serve(args, settings, console)

    return repl(settings, console, turbo_mode=bool(getattr(args, "turbo_mode", False)))


if __name__ == "__main__":
    raise SystemExit(main())
