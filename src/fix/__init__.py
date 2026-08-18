"""Public API for the pull-request monitor."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
from typing import Optional, Sequence

from rich.logging import RichHandler

from .agents import (
    AgentLauncher,
    build_agent_command,
    build_agent_prompt,
    build_conflict_prompt,
    build_review_comment_prompt,
    build_review_prompt,
    launch_conflict_agent,
)
from .agents import (
    synchronize_with_conflict_resolution as _synchronize_with_conflict_resolution,
)
from .checks import (
    fetch_review_threads,
    format_ci_check,
    format_mergeability,
    find_new_comments,
    find_new_review_threads,
    find_new_reviews,
    inspect_startup,
    log_startup_decision,
    should_synchronize_pull_request,
    summarize_checks,
)
from .cli import (
    RunDependencies,
    configure_logging,
    parse_args,
    run as _run,
    sleep_until_next_poll,
)
from .constants import (
    AGENT_EFFORT_ENV,
    AGENT_MODEL_ENV,
    DEFAULT_AGENT_EFFORT,
    DEFAULT_AGENT_MODEL,
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_INTERVAL,
    DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD,
    FAILURE_BUCKETS,
    FAILURE_KEY_VERSION,
    FAILURE_STATES,
    LOGGER,
    PASS_BUCKETS,
    PASS_STATES,
    REVIEW_KEY_VERSION,
    REVIEW_THREAD_KEY_VERSION,
    STATE_VERSION,
    SUBMITTED_REVIEW_STATES,
)
from .errors import ChecksNotReportedError, CommandError, MonitorError
from .github import CommandRunner, GitHubClient
from .models import (
    Check,
    CheckSnapshot,
    PullRequest,
    Review,
    ReviewComment,
    ReviewThread,
    StartupStatus,
)
from .monitor import Monitor as _Monitor
from .repository import (
    is_update_branch_conflict,
    is_update_branch_workflow_scope_error,
    local_git_value,
    synchronize_pull_request,
    validate_agent_checkout,
)
from .state import StateStore, default_state_path, state_lock, timestamp
from .ui import FixHighlighter, build_monitor_header, render_monitor_header


def synchronize_with_conflict_resolution(
    *,
    runner: CommandRunner,
    github: GitHubClient,
    workdir: Path,
    pull_request: PullRequest,
    state_path: Path,
    agent_launcher: AgentLauncher,
) -> PullRequest:
    """Keep the package-level synchronization hook patchable for callers."""

    return _synchronize_with_conflict_resolution(
        runner=runner,
        github=github,
        workdir=workdir,
        pull_request=pull_request,
        state_path=state_path,
        agent_launcher=agent_launcher,
        synchronize_pull_request_fn=synchronize_pull_request,
    )


class Monitor(_Monitor):
    """Monitor facade that preserves package-level synchronization hooks."""

    def __init__(
        self,
        *,
        github: GitHubClient,
        target: str,
        workdir: Path,
        state_store: StateStore,
        agent_launcher: AgentLauncher,
        runner: Optional[CommandRunner] = None,
        initial_check_snapshot: Optional[CheckSnapshot] = None,
    ) -> None:
        super().__init__(
            github=github,
            target=target,
            workdir=workdir,
            state_store=state_store,
            agent_launcher=agent_launcher,
            runner=runner,
            initial_check_snapshot=initial_check_snapshot,
            synchronize_with_conflict_resolution_fn=(
                synchronize_with_conflict_resolution
            ),
            validate_agent_checkout_fn=validate_agent_checkout,
        )


def run(
    *,
    model: str = DEFAULT_AGENT_MODEL,
    effort: str = DEFAULT_AGENT_EFFORT,
    verbose: bool = False,
    force_sync: bool = False,
) -> int:
    """Run the monitor using the package-level compatibility hooks."""

    return _run(
        model=model,
        effort=effort,
        verbose=verbose,
        force_sync=force_sync,
        dependencies=RunDependencies(
            configure_logging=configure_logging,
            command_runner_factory=CommandRunner,
            github_client_factory=GitHubClient,
            default_state_path=default_state_path,
            state_store_factory=StateStore,
            agent_launcher_factory=AgentLauncher,
            state_lock=state_lock,
            inspect_startup=inspect_startup,
            log_startup_decision=log_startup_decision,
            synchronize_with_conflict_resolution=(
                synchronize_with_conflict_resolution
            ),
            monitor_factory=Monitor,
            sleep_until_next_poll=sleep_until_next_poll,
            render_monitor_header=render_monitor_header,
        ),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        run_kwargs = {
            "model": args.model,
            "effort": args.effort,
        }
        if args.verbose:
            run_kwargs["verbose"] = True
        if args.force_sync:
            run_kwargs["force_sync"] = True
        return run(**run_kwargs)
    except KeyboardInterrupt:
        LOGGER.info("Stopped by user.")
        return 0
    except MonitorError as error:
        LOGGER.error("%s", error)
        return 1
    except Exception:
        LOGGER.exception("Unexpected error while monitoring.")
        return 1


__all__ = [
    "AGENT_EFFORT_ENV",
    "AGENT_MODEL_ENV",
    "AgentLauncher",
    "Check",
    "CheckSnapshot",
    "ChecksNotReportedError",
    "CommandError",
    "CommandRunner",
    "DEFAULT_AGENT_EFFORT",
    "DEFAULT_AGENT_MODEL",
    "DEFAULT_AGENT_TIMEOUT",
    "DEFAULT_INTERVAL",
    "DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD",
    "FAILURE_BUCKETS",
    "FAILURE_KEY_VERSION",
    "FAILURE_STATES",
    "GitHubClient",
    "FixHighlighter",
    "LOGGER",
    "Monitor",
    "MonitorError",
    "PASS_BUCKETS",
    "PASS_STATES",
    "PullRequest",
    "REVIEW_KEY_VERSION",
    "REVIEW_THREAD_KEY_VERSION",
    "Review",
    "ReviewComment",
    "ReviewThread",
    "STATE_VERSION",
    "SUBMITTED_REVIEW_STATES",
    "StartupStatus",
    "StateStore",
    "build_agent_command",
    "build_agent_prompt",
    "build_conflict_prompt",
    "build_review_comment_prompt",
    "build_review_prompt",
    "build_monitor_header",
    "configure_logging",
    "default_state_path",
    "fetch_review_threads",
    "format_ci_check",
    "format_mergeability",
    "find_new_comments",
    "find_new_review_threads",
    "find_new_reviews",
    "inspect_startup",
    "is_update_branch_conflict",
    "is_update_branch_workflow_scope_error",
    "launch_conflict_agent",
    "local_git_value",
    "log_startup_decision",
    "main",
    "parse_args",
    "run",
    "render_monitor_header",
    "should_synchronize_pull_request",
    "sleep_until_next_poll",
    "state_lock",
    "summarize_checks",
    "synchronize_pull_request",
    "synchronize_with_conflict_resolution",
    "timestamp",
    "validate_agent_checkout",
]
