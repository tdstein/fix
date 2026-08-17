from __future__ import annotations

import argparse
import dataclasses
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional, Sequence

from rich.logging import RichHandler

from .agents import AgentLauncher, synchronize_with_conflict_resolution
from .checks import inspect_startup, log_startup_decision
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
from .state import StateStore, default_state_path, state_lock
from .ui import CONSOLE, FixHighlighter, render_monitor_header as render_header


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
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


def sleep_until_next_poll(seconds: float) -> None:
    LOGGER.info("Next poll in %.0f minutes.", seconds / 60)
    time.sleep(seconds)


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
    sleep_until_next_poll: Callable[[float], None]
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
    dependencies: Optional[RunDependencies] = None,
) -> int:
    deps = dependencies or _default_dependencies()
    deps.configure_logging()
    started_at = time.monotonic()
    workdir = Path.cwd().resolve()
    runner = deps.command_runner_factory()
    github = deps.github_client_factory(cwd=workdir, runner=runner)
    initial_pull_request = github.get_pull_request()
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

    if not deps.render_monitor_header(
        initial_pull_request,
        model=model,
        effort=effort,
        interval_seconds=DEFAULT_INTERVAL,
        timeout_seconds=DEFAULT_AGENT_TIMEOUT,
    ):
        LOGGER.info(
            "Watching %s#%d @ %s (agents act only when needed).",
            initial_pull_request.repo,
            initial_pull_request.number,
            initial_pull_request.head_sha[:8],
        )
        LOGGER.info(
            "Exit when: all checks complete and no new reviews need attention.",
        )
        LOGGER.info(
            "Poll interval %.0fm; agent timeout %.0fh.",
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
            deps.log_startup_decision(startup_status)
            if startup_status.should_synchronize:
                LOGGER.info(
                    "Action: synchronizing PR #%d with base branch `%s`.",
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
                        "Synchronization complete: PR #%d advanced to %s. "
                        "Waiting for GitHub to recognize the new head and start CI.",
                        initial_pull_request.number,
                        synchronized_pull_request.head_sha[:12],
                    )
                    deps.sleep_until_next_poll(DEFAULT_INTERVAL)
                    LOGGER.info(
                        "Synchronization ready -> entering monitor.",
                    )
                else:
                    LOGGER.info(
                        "Synchronization complete: the PR head is unchanged. "
                        "Entering monitor.",
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
        first_monitor_check = True
        while True:
            if monitor.poll_once():
                elapsed = time.monotonic() - started_at
                if first_monitor_check:
                    LOGGER.info(
                        "Exit condition met on the initial check; "
                        "no interval polling needed.",
                    )
                if (
                    startup_status is not None
                    and startup_status.ci_is_green
                    and not startup_status.has_merge_conflicts
                    and startup_status.mergeability_is_known
                ):
                    LOGGER.info(
                        "Done in %.0fs - #%d @ %s is green, conflict-free, "
                        "with no new review work.",
                        elapsed,
                        initial_pull_request.number,
                        initial_pull_request.head_sha[:8],
                    )
                    LOGGER.info(
                        "Ready: checks are green, conflicts are clear, and "
                        "no new reviews need attention.",
                    )
                else:
                    LOGGER.info(
                        "Done in %.0fs - PR #%d monitoring complete.",
                        elapsed,
                        initial_pull_request.number,
                    )
                return 0
            first_monitor_check = False
            if getattr(monitor, "poll_again_immediately", False) is True:
                LOGGER.info("Polling again immediately after agent exit.")
                continue
            deps.sleep_until_next_poll(DEFAULT_INTERVAL)
