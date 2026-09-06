from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

from .errors import ChecksNotReportedError, CommandError, MonitorError
from .models import Check, PullRequest, Review, ReviewThread


def repository_from_pull_request_url(url: str) -> str:
    """Return the ``owner/name`` repository from a GitHub pull request URL."""

    parsed = urlparse(url)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.netloc.casefold() not in {"github.com", "www.github.com"}
    ):
        raise MonitorError(
            f"Expected a GitHub pull request URL, got {url!r}."
        )

    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) < 4
        or parts[2].casefold() != "pull"
        or not parts[3].isdigit()
    ):
        raise MonitorError(
            f"Expected a GitHub pull request URL, got {url!r}."
        )
    return f"{parts[0]}/{parts[1]}"


REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          comments(first: 100) {
            nodes {
              id
              body
              createdAt
              updatedAt
              url
              path
              line
              originalLine
              diffHunk
              author {
                login
              }
              commit {
                oid
              }
              replyTo {
                id
              }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
""".strip()


REVIEW_THREAD_COMMENTS_QUERY = """
query($threadId: ID!, $endCursor: String!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $endCursor) {
        nodes {
          id
          body
          createdAt
          updatedAt
          url
          path
          line
          originalLine
          diffHunk
          author {
            login
          }
          commit {
            oid
          }
          replyTo {
            id
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
""".strip()


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
        self._current_user_login: Optional[str] = None
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

    def get_current_user_login(self) -> str:
        """Return the login for the account authenticated in ``gh``."""

        if self._current_user_login is not None:
            return self._current_user_login
        output = _command_output(
            self.runner,
            ["gh", "api", "user", "--jq", ".login"],
            cwd=self.cwd,
        )
        if not output:
            raise MonitorError(
                "Could not determine the logged-in GitHub user."
            )
        self._current_user_login = output
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

    def get_review_threads(
        self,
        pull_request: PullRequest,
    ) -> list[ReviewThread]:
        """Load inline review threads, including their unresolved state."""

        repo = self.resolve_repo()
        parts = repo.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise MonitorError(
                f"Could not determine the owner and name for repository {repo!r}."
            )
        owner, name = parts
        command = [
            "gh",
            "api",
            "graphql",
            "--paginate",
            "--slurp",
            "-f",
            f"query={REVIEW_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pull_request.number}",
        ]
        result = self.runner.run(command, cwd=self.cwd)
        value = _parse_json_output(result, command)
        pages = value if isinstance(value, list) else [value]
        threads: list[ReviewThread] = []

        for page in pages:
            if not isinstance(page, Mapping):
                raise MonitorError(f"Unexpected GraphQL data from gh: {page}.")
            errors = page.get("errors")
            if errors:
                raise MonitorError(f"GitHub GraphQL request failed: {errors}.")
            data = page.get("data")
            if not isinstance(data, Mapping):
                raise MonitorError(f"Unexpected GraphQL data from gh: {page}.")
            repository = data.get("repository")
            if not isinstance(repository, Mapping):
                raise MonitorError(f"Unexpected GraphQL repository data: {page}.")
            pull_request_data = repository.get("pullRequest")
            if not isinstance(pull_request_data, Mapping):
                raise MonitorError(
                    f"Unexpected GraphQL pull request data: {page}."
                )
            connection = pull_request_data.get("reviewThreads") or {}
            if not isinstance(connection, Mapping):
                raise MonitorError(
                    f"Unexpected GraphQL review thread data: {page}."
                )
            values = connection.get("nodes") or []
            if not isinstance(values, list):
                raise MonitorError(
                    f"Unexpected GraphQL review thread nodes: {page}."
                )
            for thread in values:
                if not isinstance(thread, Mapping):
                    continue
                thread_data = dict(thread)
                thread_data["comments"] = self._load_all_review_comments(
                    thread_data,
                )
                threads.append(ReviewThread.from_json(thread_data))

        return threads

    def _load_all_review_comments(
        self,
        thread: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        comments = thread.get("comments") or {}
        if not isinstance(comments, Mapping):
            raise MonitorError(
                f"Unexpected GraphQL review comment data: {thread}."
            )
        nodes = comments.get("nodes") or []
        if not isinstance(nodes, list):
            raise MonitorError(
                f"Unexpected GraphQL review comment nodes: {thread}."
            )
        all_nodes = list(nodes)
        page_info = comments.get("pageInfo") or {}
        if not isinstance(page_info, Mapping):
            raise MonitorError(
                f"Unexpected GraphQL review comment page data: {thread}."
            )

        thread_id = str(thread.get("id") or "")
        while page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
            if not thread_id or not cursor:
                raise MonitorError(
                    f"Unexpected GraphQL review comment cursor data: {thread}."
                )
            command = [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={REVIEW_THREAD_COMMENTS_QUERY}",
                "-F",
                f"threadId={thread_id}",
                "-F",
                f"endCursor={cursor}",
            ]
            result = self.runner.run(command, cwd=self.cwd)
            value = _parse_json_output(result, command)
            if not isinstance(value, Mapping):
                raise MonitorError(f"Unexpected GraphQL data from gh: {value}.")
            errors = value.get("errors")
            if errors:
                raise MonitorError(f"GitHub GraphQL request failed: {errors}.")
            data = value.get("data")
            if not isinstance(data, Mapping):
                raise MonitorError(f"Unexpected GraphQL data from gh: {value}.")
            node = data.get("node")
            if not isinstance(node, Mapping):
                raise MonitorError(
                    f"Unexpected GraphQL review thread data: {value}."
                )
            next_comments = node.get("comments")
            if not isinstance(next_comments, Mapping):
                raise MonitorError(
                    f"Unexpected GraphQL review comment data: {value}."
                )
            next_nodes = next_comments.get("nodes") or []
            if not isinstance(next_nodes, list):
                raise MonitorError(
                    f"Unexpected GraphQL review comment nodes: {value}."
                )
            all_nodes.extend(next_nodes)
            page_info = next_comments.get("pageInfo") or {}
            if not isinstance(page_info, Mapping):
                raise MonitorError(
                    f"Unexpected GraphQL review comment page data: {value}."
                )

        return {"nodes": all_nodes}
