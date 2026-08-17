from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.highlighter import RegexHighlighter
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .models import PullRequest


FIX_THEME = Theme(
    {
        "fix.action": "bold cyan",
        "fix.pass": "bold green",
        "fix.fail": "bold red",
        "fix.wait": "bold yellow",
    }
)


class FixHighlighter(RegexHighlighter):
    """Highlight the small set of status words used by the monitor."""

    base_style = "fix."
    highlights = [
        r"(?P<pass>\bpass(?:ed)?\b)",
        r"(?P<fail>\bfail(?:ed|ure|ures)?\b|\berror\b)",
        r"(?P<wait>\bwait(?:ing|s)?\b|\bpending\b|\bunknown\b)",
        r"(?P<action>\b(?:Checking|Launching|Polling|Ready|Stopping|Watching)\b)",
    ]


CONSOLE = Console(theme=FIX_THEME, soft_wrap=True)


def _value(value: object, *, style: Optional[str] = None) -> Text:
    return Text(str(value), style=style)


def build_monitor_header(
    pull_request: PullRequest,
    *,
    model: str,
    effort: str,
    interval_seconds: float,
    timeout_seconds: float,
) -> Panel:
    """Build a compact, scannable summary of the active monitor."""

    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(ratio=1)
    pull_request_label = _value(
        f"{pull_request.repo}#{pull_request.number}",
        style="bold",
    )
    if pull_request.url:
        pull_request_label.stylize(f"link {pull_request.url}")
    table.add_row(
        _value("PR"),
        pull_request_label,
    )
    if pull_request.title:
        table.add_row(_value("Title"), _value(pull_request.title))
    table.add_row(_value("Head"), _value(pull_request.head_sha[:12], style="bold"))
    table.add_row(
        _value("Branch"),
        _value(
            f"{pull_request.head_branch} -> "
            f"{pull_request.base_branch or '(unknown base)'}"
        ),
    )
    table.add_row(_value("Agent"), _value(f"{model} ({effort})"))
    table.add_row(
        _value("Schedule"),
        _value(
            f"poll every {interval_seconds / 60:.0f}m; "
            f"agent timeout {timeout_seconds / 60 / 60:.0f}h"
        ),
    )
    table.add_row(
        _value("Exit"),
        _value("when checks are complete and no new reviews need attention"),
    )
    return Panel(
        table,
        title=Text("fix monitor", style="bold cyan"),
        border_style="cyan",
        padding=(0, 1),
        expand=False,
    )


def render_monitor_header(
    pull_request: PullRequest,
    *,
    model: str,
    effort: str,
    interval_seconds: float,
    timeout_seconds: float,
    console: Optional[Console] = None,
) -> bool:
    """Render the header only when stdout is an interactive terminal."""

    target_console = console or CONSOLE
    if not target_console.is_terminal:
        return False
    target_console.print(
        build_monitor_header(
            pull_request,
            model=model,
            effort=effort,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
        )
    )
    return True
