from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .errors import CommandError, MonitorError
from .github import CommandRunner, GitHubClient
from .models import PullRequest


def local_git_value(
    runner: CommandRunner,
    workdir: Path,
    args: Sequence[str],
) -> str:
    result = runner.run(["git", *args], cwd=workdir)
    if result.returncode != 0:
        raise CommandError(["git", *args], result.returncode, result.stderr)
    return result.stdout.strip()


def validate_agent_checkout(
    *,
    runner: CommandRunner,
    workdir: Path,
    pull_request: PullRequest,
) -> None:
    current_sha = local_git_value(runner, workdir, ["rev-parse", "HEAD"])
    if current_sha != pull_request.head_sha:
        raise MonitorError(
            "Refusing to use the checkout for an agent: "
            f"checkout is {current_sha[:12]}, PR #{pull_request.number} head is "
            f"{pull_request.head_sha[:12]}. "
            "Synchronizing cannot continue until this worktree matches the PR head. "
            f"Preserve any local changes, then run `gh pr checkout "
            f"{pull_request.number} --force` and retry."
        )

    dirty = local_git_value(runner, workdir, ["status", "--porcelain"])
    if dirty:
        raise MonitorError(
            "Refusing to use the checkout for an agent: "
            "checkout has uncommitted changes; "
            "clean it before running fix."
        )


def synchronize_pull_request(
    *,
    runner: CommandRunner,
    github: GitHubClient,
    workdir: Path,
    pull_request: PullRequest,
) -> PullRequest:
    """Update the monitored PR from its GitHub base and refresh this checkout."""

    validate_agent_checkout(
        runner=runner,
        workdir=workdir,
        pull_request=pull_request,
    )

    update_command = ["gh", "pr", "update-branch", str(pull_request.number)]
    result = runner.run(update_command, cwd=workdir)
    if result.returncode != 0:
        raise CommandError(update_command, result.returncode, result.stderr)

    checkout_command = [
        "gh",
        "pr",
        "checkout",
        str(pull_request.number),
        "--force",
    ]
    result = runner.run(checkout_command, cwd=workdir)
    if result.returncode != 0:
        raise CommandError(checkout_command, result.returncode, result.stderr)

    updated_pull_request = github.get_pull_request(str(pull_request.number))
    validate_agent_checkout(
        runner=runner,
        workdir=workdir,
        pull_request=updated_pull_request,
    )
    return updated_pull_request


def is_update_branch_conflict(error: CommandError) -> bool:
    return (
        error.command[:3] == ["gh", "pr", "update-branch"]
        and "conflict" in error.stderr.casefold()
    )


def is_update_branch_workflow_scope_error(error: CommandError) -> bool:
    stderr = error.stderr.casefold()
    return (
        error.command[:3] == ["gh", "pr", "update-branch"]
        and "workflow" in stderr
        and "scope" in stderr
        and "oauth" in stderr
    )
