"""ASCII-art banner / splash screen for the SSR TUI."""

from __future__ import annotations

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

try:
    from pyfiglet import Figlet

    _HAS_FIGLET = True
except Exception:  # pragma: no cover - pyfiglet always present in deps
    _HAS_FIGLET = False

SUBTITLE = "支持 snh48 宋昕冉 谢谢喵"

# Hand-tuned fallback so the banner still renders without pyfiglet installed.
_FALLBACK = r"""
 ____    ____    ____
/ ___|  / ___|  |  _ \
\___ \  \___ \  | |_) |
 ___) |  ___) | |  _ <
|____/  |____/  |_| \_\
"""


def render_banner(console: Console | None = None, font: str = "ansi_shadow") -> None:
    """Print the SSR welcome banner with ASCII art and the SNH48 subtitle."""
    console = console or Console()

    art: str
    if _HAS_FIGLET:
        try:
            art = Figlet(font=font).renderText("Welcome To SSR")
        except Exception:
            art = Figlet(font="standard").renderText("Welcome To SSR")
    else:
        art = _FALLBACK

    title = Text(art.rstrip("\n"), style="bold magenta")
    subtitle = Text(SUBTITLE, style="bold cyan")

    body = Text(justify="center")
    body.append_text(title)
    body.append("\n\n")
    body.append_text(subtitle)

    panel = Panel(
        Align.center(body),
        border_style="bright_magenta",
        title="[bold yellow]✨ SSR Agent ✨[/bold yellow]",
        subtitle="[dim]powered by Google ADK · gemini[/dim]",
        padding=(1, 4),
    )
    console.print(panel)
