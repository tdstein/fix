from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .errors import ChecksNotReportedError, CommandError, MonitorError
from .models import Check, PullRequest, Review


class CommandRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            check=False,
        )


def _parse_json_output(
    result: subprocess.CompletedProcess,
    command: Sequence[str],
    *,
    allow_nonzero_json: bool = False,
) -> Any:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CommandError(command, result.returncode, result.stderr) from error

    if result.returncode != 0 and not allow_nonzero_json:
        raise CommandError(command, result.returncode, result.stderr)
    return value


def _command_output(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Optional[Path] = None,
) -> str:
    result = runner.run(command, cwd=cwd)
    if result.returncode != 0:
        raise CommandError(command, result.returncode, result.stderr)
    return result.stdout.strip()


class GitHubClient:
    def __init__(
        self,
        *,
        cwd: Path,
        runner: Optional[CommandRunner] = None,
    ) -> None:
        self.cwd = cwd
        self.repo: Optional[str] = None
        self.runner = runner or CommandRunner()

    def resolve_repo(self) -> str:
        if self.repo:
            return self.repo
        output = _command_output(
            self.runner,
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=self.cwd,
        )
        if not output:
            raise MonitorError("Could not determine the current GitHub repository.")
        self.repo = output
        return output

    def get_pull_request(
        self,
        target: Optional[str] = None,
    ) -> Optional[PullRequest]:
        repo = self.resolve_repo()
        command = ["gh", "pr", "view"]
        if target is not None:
            command.append(target)
        command.extend(
            [
                "--json",
                (
                    "number,title,url,state,mergedAt,author,headRefOid,headRefName,"
                    "baseRefName,headRepository,mergeable,mergeStateStatus"
                ),
            ]
        )
        result = self.runner.run(command, cwd=self.cwd)
        if (
            target is None
            and result.returncode != 0
            and "no pull requests found for branch" in result.stderr.casefold()
        ):
            return None
        value = _parse_json_output(result, command)
        try:
            number = int(value["number"])
            author = value.get("author") or {}
            if isinstance(author, Mapping):
                author_login = str(author.get("login") or "")
            else:
                author_login = str(author or "")
            head_repository = value.get("headRepository") or {}
            if isinstance(head_repository, Mapping):
                head_repo = str(head_repository.get("nameWithOwner") or repo)
            else:
                head_repo = repo
            return PullRequest(
                repo=repo,
                number=number,
                title=str(value.get("title") or ""),
                url=str(value.get("url") or ""),
                state=str(value.get("state") or ""),
                merged_at=value.get("mergedAt"),
                head_sha=str(value["headRefOid"]),
                head_branch=str(value["headRefName"]),
                base_branch=str(value.get("baseRefName") or ""),
                head_repo=head_repo,
                author_login=author_login,
                mergeable=str(value.get("mergeable") or ""),
                merge_state_status=str(value.get("mergeStateStatus") or ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MonitorError(
                f"Unexpected pull request data from gh: {value}."
            ) from error

    def get_checks(self, pull_request: PullRequest) -> list[Check]:
        command = [
            "gh",
            "pr",
            "checks",
            str(pull_request.number),
            "--json",
            "name,state,bucket,workflow,link,startedAt,completedAt,description",
        ]
        result = self.runner.run(command, cwd=self.cwd)
        if (
            result.returncode != 0
            and "no checks reported" in result.stderr.casefold()
        ):
            raise ChecksNotReportedError(command, result.returncode, result.stderr)
        values = _parse_json_output(result, command, allow_nonzero_json=True)
        if not isinstance(values, list):
            raise MonitorError(f"Unexpected check data from gh: {values}.")
        return [Check.from_json(value) for value in values]

    def get_reviews(self, pull_request: PullRequest) -> list[Review]:
        command = [
            "gh",
            "pr",
            "view",
            str(pull_request.number),
            "--json",
            "reviews",
        ]
        result = self.runner.run(command, cwd=self.cwd)
        value = _parse_json_output(result, command)
        if not isinstance(value, Mapping):
            raise MonitorError(f"Unexpected review data from gh: {value}.")
        values = value.get("reviews")
        if not isinstance(values, list):
            raise MonitorError(f"Unexpected review data from gh: {value}.")
        return [Review.from_json(review) for review in values]
