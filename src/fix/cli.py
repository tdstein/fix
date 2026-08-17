from __future__ import annotations

import argparse
import dataclasses
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional, Sequence

from rich.live import Live
from rich.text import Text

from .agents import AgentLauncher, synchronize_with_conflict_resolution
from .checks import find_new_reviews, inspect_startup, log_startup_decision
from .constants import (
    AGENT_EFFORT_ENV,
    AGENT_MODEL_ENV,
    DEFAULT_AGENT_EFFORT,
    DEFAULT_AGENT_MODEL,
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_INTERVAL,
    LOGGER,
)
from .errors import MonitorError
from .github import CommandRunner, GitHubClient
from .monitor import Monitor
from .models import PullRequest
from .state import StateStore, default_state_path, state_lock
from .ui import (
    CONSOLE,
    FixHighlighter,
    FixRichHandler,
    render_monitor_header as render_header,
)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            FixRichHandler(
                console=CONSOLE,
                show_level=True,
                show_path=False,
                highlighter=FixHighlighter(),
                rich_tracebacks=True,
                log_time_format="[%X]",
            )
        ],
        force=True,
    )


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {remaining_seconds:02d}s"
    return f"{remaining_seconds}s"


def _idle_status(
    seconds: float,
    *,
    last_status: str,
    last_poll_at: Optional[float],
) -> Text:
    if last_poll_at is None:
        last_poll = "startup"
    else:
        last_poll = time.strftime("%H:%M", time.localtime(last_poll_at))
    return Text(
        f"⟳ next poll in {_format_duration(seconds)} "
        f"(last: {last_poll}, {last_status})"
    )


def sleep_until_next_poll(
    seconds: float,
    *,
    last_status: str = "waiting",
    last_poll_at: Optional[float] = None,
) -> None:
    if not CONSOLE.is_terminal:
        LOGGER.info(
            "⟳ next poll in %s (last: %s, %s)",
            _format_duration(seconds),
            (
                "startup"
                if last_poll_at is None
                else time.strftime("%H:%M", time.localtime(last_poll_at))
            ),
            last_status,
        )
        time.sleep(seconds)
        return

    deadline = time.monotonic() + seconds
    with Live(
        _idle_status(
            seconds,
            last_status=last_status,
            last_poll_at=last_poll_at,
        ),
        console=CONSOLE,
        refresh_per_second=1,
        transient=True,
    ) as live:
        while True:
            remaining = max(0, deadline - time.monotonic())
            live.update(
                _idle_status(
                    remaining,
                    last_status=last_status,
                    last_poll_at=last_poll_at,
                )
            )
            if remaining <= 0:
                return
            time.sleep(min(1, remaining))


def log_monitor_completion(
    *,
    monitor: Optional[Monitor] = None,
    agent_launcher: Optional[Any] = None,
    stop_reason: Optional[str] = None,
    elapsed: float,
    pull_request: PullRequest,
    startup_status: Optional[Any],
) -> None:
    if stop_reason is None and monitor is not None:
        stop_reason = getattr(monitor, "stop_reason", None)
    if isinstance(stop_reason, str) and stop_reason.startswith("CI is complete"):
        outcome = "exit condition met (CI green, no conflicts, no new reviews)"
    elif isinstance(stop_reason, str):
        outcome = f"stopped ({stop_reason})"
    elif (
        startup_status is not None
        and startup_status.ci_is_green
        and not startup_status.has_merge_conflicts
        and startup_status.mergeability_is_known
    ):
        outcome = "exit condition met (CI green, no conflicts, no new reviews)"
    else:
        outcome = "monitoring complete"

    final_pull_request = (
        getattr(monitor, "last_pull_request", None) or pull_request
    )
    poll_count = getattr(monitor, "poll_count", 0) if monitor else 0
    if not isinstance(poll_count, int):
        poll_count = 0
    agent_count = getattr(agent_launcher, "launch_count", 0)
    if not isinstance(agent_count, int):
        agent_count = getattr(monitor, "agents_launched", 0) if monitor else 0
    if not isinstance(agent_count, int):
        agent_count = 0
    final_state = (
        "merged"
        if final_pull_request.merged_at
        else final_pull_request.state.casefold()
    )
    poll_label = "poll" if poll_count == 1 else "polls"
    agent_label = "agent" if agent_count == 1 else "agents"
    completion_prefix = (
        f"✓ Done in {elapsed:.0f}s — {outcome} · "
        f"{poll_count} {poll_label} · {agent_count} {agent_label} · PR "
    )
    if CONSOLE.is_terminal and final_pull_request.url:
        LOGGER.info(
            "%s[link=%s]#%d[/link] (%s)",
            completion_prefix,
            final_pull_request.url,
            final_pull_request.number,
            final_state,
            extra={"markup": True},
        )
    else:
        LOGGER.info(
            "%s#%d (%s)",
            completion_prefix,
            final_pull_request.number,
            final_state,
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fix")
    parser.add_argument(
        "--model",
        default=os.environ.get(AGENT_MODEL_ENV) or DEFAULT_AGENT_MODEL,
        help=f"Codex model (default: ${AGENT_MODEL_ENV} or {DEFAULT_AGENT_MODEL})",
    )
    parser.add_argument(
        "--effort",
        default=os.environ.get(AGENT_EFFORT_ENV) or DEFAULT_AGENT_EFFORT,
        help=(
            f"Codex reasoning effort "
            f"(default: ${AGENT_EFFORT_ENV} or {DEFAULT_AGENT_EFFORT})"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show the full monitor configuration panel.",
    )
    return parser.parse_args(argv)


@dataclasses.dataclass(frozen=True)
class RunDependencies:
    configure_logging: Callable[[], None]
    command_runner_factory: Callable[[], CommandRunner]
    github_client_factory: Callable[..., GitHubClient]
    default_state_path: Callable[..., Path]
    state_store_factory: Callable[..., StateStore]
    agent_launcher_factory: Callable[..., AgentLauncher]
    state_lock: Callable[..., Any]
    inspect_startup: Callable[..., Any]
    log_startup_decision: Callable[..., None]
    synchronize_with_conflict_resolution: Callable[..., Any]
    monitor_factory: Callable[..., Monitor]
    sleep_until_next_poll: Callable[..., None]
    render_monitor_header: Callable[..., bool] = render_header


def _default_dependencies() -> RunDependencies:
    return RunDependencies(
        configure_logging=configure_logging,
        command_runner_factory=CommandRunner,
        github_client_factory=GitHubClient,
        default_state_path=default_state_path,
        state_store_factory=StateStore,
        agent_launcher_factory=AgentLauncher,
        state_lock=state_lock,
        inspect_startup=inspect_startup,
        log_startup_decision=log_startup_decision,
        synchronize_with_conflict_resolution=synchronize_with_conflict_resolution,
        monitor_factory=Monitor,
        sleep_until_next_poll=sleep_until_next_poll,
        render_monitor_header=render_header,
    )


def run(
    *,
    model: str = DEFAULT_AGENT_MODEL,
    effort: str = DEFAULT_AGENT_EFFORT,
    verbose: bool = False,
    dependencies: Optional[RunDependencies] = None,
) -> int:
    deps = dependencies or _default_dependencies()
    deps.configure_logging()
    started_at = time.monotonic()
    workdir = Path.cwd().resolve()
    runner = deps.command_runner_factory()
    github = deps.github_client_factory(cwd=workdir, runner=runner)
    initial_pull_request = github.get_pull_request()
    if initial_pull_request is None:
        LOGGER.info("No pull request found for the current branch; nothing to fix.")
        return 0
    target = str(initial_pull_request.number)
    state_path = deps.default_state_path(
        initial_pull_request.repo,
        initial_pull_request.number,
    )
    state_store = deps.state_store_factory(state_path)
    agent_launcher = deps.agent_launcher_factory(
        workdir=workdir,
        model=model,
        effort=effort,
    )

    header_kwargs = {
        "model": model,
        "effort": effort,
        "interval_seconds": DEFAULT_INTERVAL,
        "timeout_seconds": DEFAULT_AGENT_TIMEOUT,
    }
    if verbose:
        header_kwargs["verbose"] = True
    if not deps.render_monitor_header(initial_pull_request, **header_kwargs):
        LOGGER.info(
            "Watching %s#%d @ %s.",
            initial_pull_request.repo,
            initial_pull_request.number,
            initial_pull_request.head_sha[:8],
        )
        LOGGER.info(
            "⟳ poll every %.0fm · agent timeout %.0fh",
            DEFAULT_INTERVAL / 60,
            DEFAULT_AGENT_TIMEOUT / 60 / 60,
        )
    startup_status: Optional[Any] = None
    initial_check_snapshot = None
    with deps.state_lock(state_path):
        if initial_pull_request.is_open:
            startup_status = deps.inspect_startup(
                github=github,
                pull_request=initial_pull_request,
            )
            initial_check_snapshot = startup_status.check_snapshot
            if (
                startup_status.ci_is_green
                and not startup_status.has_merge_conflicts
                and startup_status.mergeability_is_known
            ):
                state = state_store.load()
                new_reviews = find_new_reviews(
                    reviews=github.get_reviews(initial_pull_request),
                    pull_request=initial_pull_request,
                    seen_reviews=state.get("seen_reviews", {}),
                )
                if not new_reviews:
                    elapsed = time.monotonic() - started_at
                    log_monitor_completion(
                        stop_reason=(
                            f"CI is complete with {startup_status.total_checks} "
                            f"{'check' if startup_status.total_checks == 1 else 'checks'} "
                            "and no new reviews."
                        ),
                        elapsed=elapsed,
                        pull_request=initial_pull_request,
                        startup_status=startup_status,
                        agent_launcher=agent_launcher,
                    )
                    return 0
            deps.log_startup_decision(startup_status)
            if startup_status.should_synchronize:
                LOGGER.info(
                    "➜ synchronizing PR #%d with base branch `%s`.",
                    initial_pull_request.number,
                    initial_pull_request.base_branch or "(unknown)",
                )
                synchronized_pull_request = deps.synchronize_with_conflict_resolution(
                    runner=runner,
                    github=github,
                    workdir=workdir,
                    pull_request=initial_pull_request,
                    state_path=state_path,
                    agent_launcher=agent_launcher,
                )
                if synchronized_pull_request.head_sha != initial_pull_request.head_sha:
                    initial_check_snapshot = None
                    LOGGER.info(
                        "… PR #%d advanced to %s; waiting for GitHub to start CI.",
                        initial_pull_request.number,
                        synchronized_pull_request.head_sha[:12],
                    )
                    deps.sleep_until_next_poll(DEFAULT_INTERVAL)
                    LOGGER.info(
                        "⟳ monitoring synchronized head",
                    )
                else:
                    LOGGER.info(
                        "⟳ monitoring unchanged head",
                    )
        monitor = deps.monitor_factory(
            github=github,
            target=target,
            workdir=workdir,
            state_store=state_store,
            agent_launcher=agent_launcher,
            runner=runner,
            initial_check_snapshot=initial_check_snapshot,
        )
        while True:
            if monitor.poll_once():
                elapsed = time.monotonic() - started_at
                log_monitor_completion(
                    monitor=monitor,
                    agent_launcher=agent_launcher,
                    elapsed=elapsed,
                    pull_request=initial_pull_request,
                    startup_status=startup_status,
                )
                return 0
            if getattr(monitor, "poll_again_immediately", False) is True:
                LOGGER.info("⟳ polling immediately after agent exit")
                continue
            deps.sleep_until_next_poll(
                DEFAULT_INTERVAL,
                last_status=getattr(monitor, "last_status", "waiting"),
                last_poll_at=getattr(monitor, "last_poll_at", None),
            )
