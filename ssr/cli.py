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


def cmd_init(args, settings: Settings, console: Console) -> int:
    _first_run_setup(settings, console)
    install_builtin_skills(settings.skills_dir, force=True)

    import subprocess
    anthropic_dir = settings.skills_dir / "anthropic"
    if not anthropic_dir.exists():
        console.print("[cyan]Installing anthropic official skills...[/cyan]")
        subprocess.run(["git", "clone", "https://github.com/anthropics/skills", str(anthropic_dir)], capture_output=True)

    console.print(f"[green]✓[/green] SSR home ready at [bold]{settings.home}[/bold]")
    console.print(f"  Edit [bold]{settings.env_file}[/bold] to add GEMINI_API_KEY / TAVILY_API_KEY")
    return 0


def cmd_ask(args, settings: Settings, console: Console) -> int:
    from .agent.core import SSRAgent

    if args.cwd:
        settings = load_settings(args.cwd)
    settings.extra["turbo_mode"] = getattr(args, "turbo_mode", False) or getattr(args, "bypass_permissions", False)
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


def repl(settings: Settings, console: Console) -> int:
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
            console.print("\n[dim]bye 谢谢喵[/dim]")
            return 0
        if not line:
            continue
        if line.startswith("/"):
            if not handle_slash(line, agent, settings, console):
                console.print("[dim]bye 谢谢喵[/dim]")
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
    p.add_argument("--turbo-mode", action="store_true", help="bypass execution permissions")
    p.add_argument("--bypass-permissions", action="store_true", help="bypass execution permissions")
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

    serve_p = sub.add_parser("serve", help="run a standard OpenAI compatible REST API server")
    serve_p.add_argument("--port", type=int, default=8000)

    chan = sub.add_parser("channel", help="configure / serve a channel bot (feishu/wechat)")
    csub = chan.add_subparsers(dest="channel_action", required=True)
    c_conf = csub.add_parser("config")
    c_conf.add_argument("name", choices=["feishu", "wechat"])
    c_on = csub.add_parser("on")
    c_on.add_argument("name", choices=["feishu", "wechat"])

    rc = sub.add_parser("rc", help="remote control: connect this instance to a dispatch server")
    rc.add_argument("--reconfigure", action="store_true", help="re-run interactive setup")
    rcsub = rc.add_subparsers(dest="rc_action")
    rcsub.add_parser("configure", help="set dispatch endpoint, token, node name and tags")
    rcsub.add_parser("status", help="show the saved remote-control configuration")
    rctags = rcsub.add_parser("tags", help="set this node's tags (comma/space separated)")
    rctags.add_argument("tags", nargs="+", help="tags, e.g. prod gpu  or  prod,gpu")

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
    settings.extra["turbo_mode"] = getattr(args, "turbo_mode", False) or getattr(args, "bypass_permissions", False)

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
    if args.command == "serve":
        from .server import serve
        return serve(args.port)
    if args.command == "channel":
        if args.channel_action == "config":
            if args.name == "feishu":
                from .integrations import feishu
                return feishu.configure_interactive(settings)
            elif args.name == "wechat":
                console.print("[cyan]WeChat integration does not require explicit config. It authenticates via QR code at runtime.[/cyan]")
                return 0
        elif args.channel_action == "on":
            if args.name == "feishu":
                from .integrations import feishu
                return feishu.serve_long_connection(settings)
            elif args.name == "wechat":
                from .integrations import wechat
                return wechat.serve_long_connection(settings)
    if args.command == "rc":
        return cmd_rc(args, settings, console)

    return repl(settings, console)


if __name__ == "__main__":
    raise SystemExit(main())
