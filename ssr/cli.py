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
from .plugins import install_builtin_plugins


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

# Event bus (optional). Every main process starts a non-blocking bus server so
# external scripts / other agents can connect; set an api key to require auth.
# SSR_BUS_API_KEY=
# SSR_BUS_HOST=127.0.0.1
# SSR_BUS_PORT=8765
# SSR_BUS_SERVE=1          # set 0 to disable the embedded server
# SSR_BUS_URL=             # bridge to an external bus server instead
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
            elif ch.name == "xiaomi":
                from ssr.integrations.xiaomi import load_config as _load_xiaomi
                has_config = _load_xiaomi(settings) is not None
            status = "[green]configured[/green]" if has_config else "[yellow]not configured[/yellow]"
            console.print(f"Channel [bold cyan]{ch.name}[/bold cyan]: {status}")
        return 0
        
    return 0


def _render_stats_table(gw, settings, names: list[str], with_cpu: bool):
    from rich.table import Table

    mgr = gw.get_manager()
    table = Table(title=f"SSR Gateway resource usage ({mgr.backend})")
    table.add_column("Name", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("PID", justify="right", style="dim")
    table.add_column("Memory (RSS)", justify="right", style="magenta")
    table.add_column("Threads", justify="right")
    if with_cpu:
        table.add_column("CPU %", justify="right", style="yellow")
    table.add_column("Source", style="dim")
    for name in names:
        st = gw.gather_stats(settings, name, with_cpu=with_cpu)
        running = "[green]running[/green]" if st.get("running") else "[yellow]stopped[/yellow]"
        row = [
            name, running,
            str(st.get("pid") or "-"),
            gw.format_bytes(st.get("rss")),
            str(st.get("threads") or "-"),
        ]
        if with_cpu:
            cpu = st.get("cpu_percent")
            row.append(f"{cpu:.1f}" if cpu is not None else "-")
        row.append(st.get("source") or "-")
        table.add_row(*row)
    return table


def _gateway_stats(args, settings: Settings, console: Console) -> int:
    from .integrations import gateway as gw

    gateways = gw.load_gateways(settings)
    if args.name:
        if args.name not in gateways:
            console.print(f"[red]No gateway named '{args.name}'.[/red]")
            return 1
        names = [args.name]
    else:
        names = list(gateways)
    if not names:
        console.print("[yellow]No gateways installed. Create one with: ssr gateway install <name> --channel <channel>[/yellow]")
        return 0

    with_cpu = bool(getattr(args, "cpu", False))
    if not getattr(args, "watch", False):
        console.print(_render_stats_table(gw, settings, names, with_cpu))
        return 0

    # Live monitoring: refresh until interrupted.
    import time
    from rich.live import Live

    interval = max(0.5, float(getattr(args, "interval", 2.0)))
    try:
        with Live(_render_stats_table(gw, settings, names, with_cpu),
                  console=console, refresh_per_second=4) as live:
            while True:
                time.sleep(interval)
                live.update(_render_stats_table(gw, settings, names, with_cpu))
    except KeyboardInterrupt:
        console.print("[dim]stopped.[/dim]")
    return 0


def cmd_gateway(args, settings: Settings, console: Console) -> int:
    from .integrations import gateway as gw

    action = args.gateway_action

    if action == "run":
        # Foreground entrypoint used by the installed system service.
        return gw.run_gateway(settings, args.name)

    if action == "install":
        if args.channel not in gw.VALID_CHANNELS:
            console.print(f"[red]Unknown channel '{args.channel}'. Choose from: {', '.join(gw.VALID_CHANNELS)}[/red]")
            return 1
        record = gw.Gateway(name=args.name, channel=args.channel, cwd=args.cwd or "")
        gw.add_gateway(settings, record)
        mgr = gw.get_manager()
        console.print(f"[dim]service backend: {mgr.backend}[/dim]")
        msg = mgr.install(settings, record, start=not args.no_start)
        console.print(f"[green]✓[/green] Gateway [bold]{args.name}[/bold] → channel [cyan]{args.channel}[/cyan]")
        console.print(msg)
        return 0

    if action == "uninstall":
        mgr = gw.get_manager()
        console.print(mgr.uninstall(settings, args.name))
        if gw.remove_gateway(settings, args.name):
            console.print(f"[green]✓[/green] Gateway '{args.name}' removed.")
        else:
            console.print(f"[yellow]No gateway record named '{args.name}'.[/yellow]")
        return 0

    if action in ("start", "stop", "restart"):
        if gw.get_gateway(settings, args.name) is None:
            console.print(f"[red]No gateway named '{args.name}'. Install it first.[/red]")
            return 1
        mgr = gw.get_manager()
        console.print(getattr(mgr, action)(settings, args.name))
        return 0

    if action == "status":
        mgr = gw.get_manager()
        gateways = gw.load_gateways(settings)
        if args.name:
            gateways = {args.name: gateways[args.name]} if args.name in gateways else {}
            if not gateways:
                console.print(f"[red]No gateway named '{args.name}'.[/red]")
                return 1
        if not gateways:
            console.print("[yellow]No gateways installed. Create one with: ssr gateway install <name> --channel <channel>[/yellow]")
            return 0
        from rich.table import Table
        table = Table(title=f"SSR Gateways ({mgr.backend})")
        table.add_column("Name", style="cyan")
        table.add_column("Channel", style="green")
        table.add_column("Status", style="bold")
        table.add_column("Memory", style="magenta", justify="right")
        table.add_column("CWD", style="dim")
        for name, record in gateways.items():
            st = gw.gather_stats(settings, name)
            table.add_row(name, record.channel, mgr.status(settings, name),
                          gw.format_bytes(st.get("rss")), record.cwd or "-")
        console.print(table)
        return 0

    if action == "stats":
        return _gateway_stats(args, settings, console)

    if action == "list":
        gateways = gw.load_gateways(settings)
        if not gateways:
            console.print("[yellow]No gateways installed.[/yellow]")
            return 0
        for name, record in gateways.items():
            console.print(f"- [bold cyan]{name}[/bold cyan] → channel [green]{record.channel}[/green]"
                          + (f"  cwd={record.cwd}" if record.cwd else ""))
        return 0

    return 0


def cmd_acp(settings: Settings) -> int:
    from .integrations.acp import run_acp

    run_acp(settings)
    return 0


def cmd_rc(args, settings: Settings, console: Console) -> int:
    console.print(
        "[red]rc(dispatch) is deprecated, the new agent bus protocol will be "
        "implemented in next version.[/red]"
    )
    return 1


def is_actual_api_key(value: str) -> bool:
    """Detect if value is an actual API key rather than an environment variable name."""
    import re
    if not value:
        return False
    # If it starts with common API key prefixes, it's definitely an API key
    if value.startswith(("sk-", "AIza")):
        return True
    # If it contains lowercase letters and is longer than 20 characters
    if any(c.islower() for c in value) and len(value) > 20:
        return True
    # Env var names are typically uppercase alphanumeric plus underscores.
    # If it has characters that are not allowed in environment variables (like dots, hyphens, slashes, spaces)
    if not re.match(r"^[a-zA-Z0-9_]+$", value):
        return True
    return False


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
            table.add_column("Direct Key", style="cyan")
            table.add_column("Base URL", style="blue")
            table.add_column("Type", style="bold red")

            for m in cfg.list_models():
                is_primary = "Primary" if m.id == cfg.primary else "Fallback"
                key = getattr(m, "api_key", None)
                masked_key = f"{key[:4]}...{key[-4:]}" if key and len(key) > 8 else ("****" if key else "-")
                table.add_row(
                    m.id,
                    m.provider,
                    m.model,
                    m.api_key_env,
                    masked_key,
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

                    is_direct_key = False
                    api_key = None
                    if is_actual_api_key(api_key_env):
                        api_key = api_key_env
                        default_envs = {
                            "gemini": "GEMINI_API_KEY",
                            "anthropic": "ANTHROPIC_API_KEY",
                            "openai": "OPENAI_API_KEY"
                        }
                        env_name = default_envs.get(provider, "API_KEY")
                        console.print(f"[yellow]Detected that the input looks like an actual API key. Saving as direct API key and setting API key environment variable name to '{env_name}'.[/yellow]")
                        api_key_env = env_name
                        is_direct_key = True

                    if not is_direct_key:
                        api_key_input = input("Enter API key directly (optional, press Enter to use environment variable): ").strip()
                        api_key = api_key_input if api_key_input else None

                    base_url = input("Enter base URL (optional, press Enter to skip): ").strip()
                    base_url = base_url if base_url else None

                    entry = ModelEntry(
                        id=model_id,
                        provider=provider,
                        model=model_name,
                        api_key_env=api_key_env,
                        api_key=api_key,
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


_EMBEDDED_BUS_SERVER = None  # keep a reference so the server thread isn't GC'd


def ensure_bus_server(settings: Settings, console: Console | None = None) -> None:
    """Start this main process's embedded, non-blocking bus server.

    Best-effort and idempotent. Honours ``settings.bus_serve`` (on by default).
    If the port is already bound — another ``ssr`` process owns the server — we
    just reuse it. Either way ``settings.bus_url`` is pointed at the server so
    the agent bridges its in-process bus to it (with api-key auth when set).
    """
    global _EMBEDDED_BUS_SERVER
    if not getattr(settings, "bus_serve", False):
        return
    if settings.bus_url:  # an explicit remote bus was configured; defer to it
        return
    if _EMBEDDED_BUS_SERVER is not None:
        return
    host, port = settings.bus_host, settings.bus_port
    url = f"ws://{host}:{port}"
    from .bus.server import serve_in_thread

    try:
        _EMBEDDED_BUS_SERVER, _ = serve_in_thread(host, port, api_key=settings.bus_api_key)
        if console:
            note = " (api-key required)" if settings.bus_api_key else ""
            console.print(f"[dim]bus server listening on {url}{note}[/dim]")
    except OSError:
        # Port already in use → an existing ssr bus server; bridge to it instead.
        if console:
            console.print(f"[dim]bus already running at {url}; bridging to it[/dim]")
    except Exception as e:  # never let bus setup break startup
        if console:
            console.print(f"[yellow]bus server could not start: {e}[/yellow]")
        return
    settings.bus_url = url


def cmd_bus(args, settings: Settings, console: Console) -> int:
    """Run / interact with the SSR event bus."""
    action = args.bus_action

    api_key = getattr(args, "api_key", None) or settings.bus_api_key

    if action == "serve":
        from .bus.server import run_server as run_bus_server

        note = " [dim](api-key required)[/dim]" if api_key else ""
        console.print(f"[cyan]Starting SSR bus server on ws://{args.host}:{args.port}[/cyan]{note}")
        try:
            run_bus_server(host=args.host, port=args.port, api_key=api_key)
        except Exception as e:
            console.print(f"[red]Bus server error: {e}[/red]")
            return 1
        return 0

    url = getattr(args, "url", None) or settings.bus_url or "ws://127.0.0.1:8765"
    from .bus import BusClient

    if action == "send":
        import json

        raw = args.payload
        if not raw:
            payload = {}
        else:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # Not valid JSON: either a plain string, or the shell mangled the
                # quotes (common on Windows PowerShell, which strips the inner
                # double quotes of '{"k":"v"}'). Degrade gracefully by sending
                # the raw text as a {"text": ...} payload instead of erroring.
                console.print(
                    "[yellow]Payload is not valid JSON; sending it as "
                    '{"text": ...}.[/yellow]\n'
                    "[dim]Tip (PowerShell): wrap JSON in single quotes and escape "
                    'inner quotes, e.g. \'{\\"msg\\": \\"hi\\"}\', or use the '
                    "stop-parsing token: ssr bus send topic --% '{\"msg\":\"hi\"}'[/dim]"
                )
                payload = {"text": raw}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        client = BusClient(url, source="ssr-cli", api_key=api_key)
        try:
            client.connect()
            event = client.publish(args.topic, payload)
            console.print(f"[green]Published[/green] {event.get('id', '')} on '{args.topic}'.")
        except Exception as e:
            console.print(f"[red]Could not send: {e}[/red]")
            if isinstance(e, OSError):
                console.print(
                    f"[dim]Is a bus server running at {url}? "
                    "Start one with [bold]ssr bus serve[/bold].[/dim]"
                )
            return 1
        finally:
            client.close()
        return 0

    if action == "listen":
        client = BusClient(url, source="ssr-cli", api_key=api_key)
        seen = {"n": 0}
        import threading

        stop = threading.Event()

        def _on(ev):
            console.print(f"[cyan]●[/cyan] [bold]{ev.topic}[/bold] [dim]({ev.source})[/dim] {ev.payload}")
            seen["n"] += 1
            if args.count and seen["n"] >= args.count:
                stop.set()

        try:
            client.connect()
            client.subscribe(args.pattern, _on)
            console.print(f"[dim]Listening on '{args.pattern}' at {url} (Ctrl-C to stop)…[/dim]")
            stop.wait()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            console.print(f"[red]Could not listen: {e}[/red]")
            return 1
        finally:
            client.close()
        return 0

    if action == "status":
        client = BusClient(url, source="ssr-cli", api_key=api_key)
        try:
            client.connect()
            info = client.ping()
            console.print(f"[green]Bus online[/green] at {url}: {info.get('peers', 0)} peer(s).")
            events = client.history("**", 10)
            if events:
                console.print("[dim]Recent events:[/dim]")
                for e in events:
                    console.print(f"  - {e.topic} ({e.source})")
        except Exception as e:
            console.print(f"[red]Bus unreachable at {url}: {e}[/red]")
            return 1
        finally:
            client.close()
        return 0

    console.print("[yellow]Usage: ssr bus <serve|send|listen|status>[/yellow]")
    return 1


def cmd_serve(args, settings: Settings, console: Console) -> int:
    from .models import ModelsConfig
    cfg = ModelsConfig(settings)
    primary = cfg.get_primary()

    import os
    has_api_key = getattr(primary, "api_key", None) or os.environ.get(primary.api_key_env)
    if not has_api_key:
        console.print(f"[red]Error: Primary model '{primary.id}' requires either a direct API key or the environment variable '{primary.api_key_env}', but neither is set.[/red]")
        console.print(f"Please configure a direct API key using 'ssr models config' or set the environment variable in ~/.ssr/.env.")
        return 1

    from .serve import run_server
    run_server(settings, host=args.host, port=args.port)
    return 0


class WindowsStdinReader:
    def __init__(self):
        self.buffer = []

    def poll_line(self) -> str | None:
        """Polls for a line of input. Returns the line (without newline) if complete, else None."""
        import msvcrt
        import sys
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):
                # Read the next character which is the actual code of the special key
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if ch == '\r' or ch == '\n':
                line = "".join(self.buffer)
                self.buffer = []
                print()  # print a newline to echo the enter key
                return line
            elif ch == '\b':
                if self.buffer:
                    self.buffer.pop()
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
            elif ch == '\x03':  # Ctrl+C
                raise KeyboardInterrupt
            elif ch == '\x04':  # Ctrl+D
                raise EOFError
            elif ord(ch) >= 32:
                self.buffer.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
        return None


def _run_turn_with_controls(agent, line: str, console: Console) -> str:
    """Run a main agent turn while accepting ``/stop`` and ``/btw`` concurrently.

    The turn runs on a worker thread; the main thread polls stdin (non-blocking
    on POSIX) so the user can interrupt the task (``/stop``) or ask a side
    question that the agent answers in parallel (``/btw <question>``) without
    racing the main turn's state.
    """
    import select
    import sys
    import threading

    holder: dict = {}

    def worker() -> None:
        try:
            holder["reply"] = agent.run(line)
        except Exception as e:  # pragma: no cover - defensive
            holder["reply"] = f"[agent error] {e}"

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    is_win_tty = (sys.platform == "win32" and sys.stdin is not None and sys.stdin.isatty())
    can_poll = hasattr(select, "select") and sys.stdin is not None and sys.stdin.isatty()

    agent._is_polling_stdin = can_poll or is_win_tty

    win_reader = None
    if is_win_tty:
        win_reader = WindowsStdinReader()

    try:
        while t.is_alive():
            ctrl = None
            if win_reader:
                ctrl = win_reader.poll_line()
                if ctrl is None:
                    t.join(timeout=0.05)
                    continue
            elif can_poll:
                try:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                except Exception:
                    can_poll = False
                    ready = []
                if ready:
                    ctrl = sys.stdin.readline().strip()
            else:
                t.join(timeout=0.2)
                continue

            if not ctrl:
                continue

            from .approval import get_active_approval, ApprovalDecision
            approval = get_active_approval()

            if approval:
                choice = ctrl.strip()
                if choice.startswith("/"):
                    cmd_parts = choice.split()
                    cmd_lower = cmd_parts[0].lower()
                    if cmd_lower in ("/approve", "/ok", "/yes"):
                        approval.decision = ApprovalDecision.ALLOW_ONCE
                        approval.event.set()
                        console.print("[green]Command approved (once).[/green]")
                    elif cmd_lower in ("/alwaysallow", "/always"):
                        approval.decision = ApprovalDecision.ALWAYS_ALLOW
                        approval.event.set()
                        console.print("[green]Command pattern always allowed.[/green]")
                    elif cmd_lower == "/disallow" or cmd_lower.startswith("/disallow"):
                        reason = " ".join(cmd_parts[1:]) if len(cmd_parts) > 1 else "Denied by user"
                        approval.decision = ApprovalDecision.DENY
                        approval.reason = reason
                        approval.event.set()
                        console.print(f"[red]Command disallowed: {reason}[/red]")
                    elif cmd_lower in ("/stop", "/cancel"):
                        agent.request_stop()
                        approval.decision = ApprovalDecision.DENY
                        approval.reason = "Stop requested"
                        approval.event.set()
                        console.print("[yellow]⏹ stop requested — finishing the current step…[/yellow]")
                    else:
                        from .slash import handle as handle_slash
                        if not handle_slash(choice, agent, agent.settings, console):
                            agent.request_stop()
                            approval.decision = ApprovalDecision.DENY
                            approval.event.set()
                            console.print("[dim]bye[/dim]")
                            import os
                            os._exit(0)
                else:
                    if choice == "1":
                        approval.decision = ApprovalDecision.ALLOW_ONCE
                        approval.event.set()
                        console.print("[green]Command approved (once).[/green]")
                    elif choice == "2":
                        approval.decision = ApprovalDecision.ALWAYS_ALLOW
                        approval.event.set()
                        console.print("[green]Command pattern always allowed.[/green]")
                    elif choice.startswith("3"):
                        reason = choice[1:].strip()
                        if not reason:
                            try:
                                reason = input("Reason / what to do instead (optional): ").strip()
                            except (KeyboardInterrupt, EOFError):
                                reason = ""
                        approval.decision = ApprovalDecision.DENY
                        approval.reason = reason or "Denied by user"
                        approval.event.set()
                        console.print(f"[red]Command disallowed: {approval.reason}[/red]")
                    else:
                        console.print("[yellow]Invalid choice. Please enter 1, 2, or 3, or type a slash command (e.g. /approve, /disallow).[/yellow]")
            else:
                low = ctrl.lower()
                if low in ("/stop", "/cancel"):
                    if agent.request_stop():
                        console.print("[yellow]⏹ stop requested — finishing the current step…[/yellow]")
                    else:
                        console.print("[dim]Nothing is running.[/dim]")
                elif low == "/btw" or low.startswith("/btw "):
                    question = ctrl[4:].strip()
                    if not question:
                        console.print("[yellow]Usage: /btw <question>[/yellow]")
                        continue

                    def side(q: str = question) -> None:
                        ans = agent.answer_side_question(q)
                        console.print(f"\n[bold cyan]btw ▸[/bold cyan] {ans}\n")

                    threading.Thread(target=side, daemon=True).start()
                    console.print("[dim cyan]↳ answering your side question…[/dim cyan]")
                elif ctrl.startswith("/"):
                    from .slash import handle as handle_slash
                    if not handle_slash(ctrl, agent, agent.settings, console):
                        agent.request_stop()
                        console.print("[dim]bye[/dim]")
                        import os
                        os._exit(0)
                else:
                    console.print(
                        "[dim](busy — use /stop to interrupt or /btw <question> to ask alongside)[/dim]"
                    )
    finally:
        agent._is_polling_stdin = False
    t.join()
    return holder.get("reply", "")


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
        console.print(
            "[dim magenta]thinking…[/dim magenta] "
            "[dim](type /stop to interrupt, or /btw <question> to ask alongside)[/dim]"
        )
        reply = _run_turn_with_controls(agent, line, console)
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

    chan = sub.add_parser("channel", help="configure / run messaging channels (Feishu, WeChat, XiaoAI)")
    chsub = chan.add_subparsers(dest="channel_action", required=True)
    chcfg = chsub.add_parser("config", aliases=["configure"], help="configure a channel")
    chcfg.add_argument("channel_name", choices=["feishu", "wechat", "xiaomi"])
    chon = chsub.add_parser("on", aliases=["serve"], help="start listening on channel(s)")
    chon.add_argument("channel_name", choices=["feishu", "wechat", "xiaomi", "all"], nargs="?", default="all")
    chsub.add_parser("list", help="list registered channels")
    chsub.add_parser("status", help="show channels configuration status")

    # rc / remote dispatch was removed; keep the command so it prints a clear
    # deprecation notice instead of an "unknown command" error.
    rc = sub.add_parser("rc", help="(deprecated) remote dispatch — removed")
    rc.add_argument("rc_args", nargs="*", help=argparse.SUPPRESS)

    models = sub.add_parser("models", help="manage LLM models configuration")
    models_sub = models.add_subparsers(dest="models_action", required=True)
    models_sub.add_parser("config", aliases=["configure"], help="interactively configure models")

    serve = sub.add_parser("serve", help="start an OpenAI-compatible HTTP API server")
    serve.add_argument("--host", default="127.0.0.1", help="host to bind the server to")
    serve.add_argument("--port", type=int, default=8000, help="port to bind the server to")

    bus = sub.add_parser("bus", help="run / talk to the async event bus (JSON-RPC over WebSocket)")
    bsub = bus.add_subparsers(dest="bus_action", required=True)
    bserve = bsub.add_parser("serve", help="run a remote bus server")
    bserve.add_argument("--host", default="127.0.0.1", help="host to bind")
    bserve.add_argument("--port", type=int, default=8765, help="port to bind")
    bserve.add_argument("--api-key", dest="api_key", help="require this api key (default: $SSR_BUS_API_KEY)")
    bsend = bsub.add_parser("send", help="publish an event to a bus server")
    bsend.add_argument("topic")
    bsend.add_argument("payload", nargs="?", default="", help="JSON object payload")
    bsend.add_argument("--url", help="bus server URL (default: $SSR_BUS_URL or ws://127.0.0.1:8765)")
    bsend.add_argument("--api-key", dest="api_key", help="bus api key (default: $SSR_BUS_API_KEY)")
    blisten = bsub.add_parser("listen", help="subscribe and print matching events")
    blisten.add_argument("pattern", nargs="?", default="**", help="topic pattern, e.g. 'task.*'")
    blisten.add_argument("--url", help="bus server URL")
    blisten.add_argument("--api-key", dest="api_key", help="bus api key (default: $SSR_BUS_API_KEY)")
    blisten.add_argument("--count", type=int, default=0, help="exit after N events (0 = forever)")
    bstatus = bsub.add_parser("status", help="ping a bus server and show recent events")
    bstatus.add_argument("--url", help="bus server URL")
    bstatus.add_argument("--api-key", dest="api_key", help="bus api key (default: $SSR_BUS_API_KEY)")

    gw = sub.add_parser("gateway", help="install a channel-bound SSR instance as a system service")
    gwsub = gw.add_subparsers(dest="gateway_action", required=True)
    gwi = gwsub.add_parser("install", help="install (and start) a gateway as a system service")
    gwi.add_argument("name")
    gwi.add_argument("--channel", required=True, choices=["feishu", "wechat", "xiaomi", "all"],
                     help="channel this gateway serves")
    gwi.add_argument("--cwd", help="starting working directory for the agent")
    gwi.add_argument("--no-start", action="store_true", help="install the service but do not start it")
    gwu = gwsub.add_parser("uninstall", help="stop and remove a gateway service")
    gwu.add_argument("name")
    gwst = gwsub.add_parser("start", help="start an installed gateway service")
    gwst.add_argument("name")
    gwsp = gwsub.add_parser("stop", help="stop a running gateway service")
    gwsp.add_argument("name")
    gwr = gwsub.add_parser("restart", help="restart a gateway service")
    gwr.add_argument("name")
    gwstat = gwsub.add_parser("status", help="show gateway service status")
    gwstat.add_argument("name", nargs="?")
    gwstats = gwsub.add_parser("stats", help="show live memory / resource usage of a gateway")
    gwstats.add_argument("name", nargs="?")
    gwstats.add_argument("--watch", action="store_true", help="refresh continuously until Ctrl-C")
    gwstats.add_argument("--interval", type=float, default=2.0, help="watch refresh interval in seconds")
    gwstats.add_argument("--cpu", action="store_true", help="also sample CPU%% (needs psutil)")
    gwsub.add_parser("list", help="list configured gateways")
    gwrun = gwsub.add_parser("run", help="run a gateway in the foreground (used by the system service)")
    gwrun.add_argument("name")

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
        err_console = Console(stderr=True)
        _first_run_setup(settings, err_console)
        ensure_bus_server(settings, err_console)
        return cmd_acp(settings)

    _first_run_setup(settings, console)
    # The main process hosts a non-blocking bus server so external scripts and
    # other agents can talk to this agent's bus. Skip it for commands that are
    # not running an agent (or are the bus server themselves).
    if args.command not in ("init", "index", "bus", "task", "models"):
        ensure_bus_server(settings, console)
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
    if args.command == "bus":
        return cmd_bus(args, settings, console)
    if args.command == "gateway":
        return cmd_gateway(args, settings, console)

    return repl(settings, console, turbo_mode=bool(getattr(args, "turbo_mode", False)))


if __name__ == "__main__":
    raise SystemExit(main())
