from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, Mapping, Optional

from .constants import (
    FAILURE_BUCKETS,
    FAILURE_KEY_VERSION,
    FAILURE_STATES,
    PASS_BUCKETS,
    PASS_STATES,
    REVIEW_KEY_VERSION,
    REVIEW_THREAD_KEY_VERSION,
    SUBMITTED_REVIEW_STATES,
)


@dataclasses.dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    state: str
    merged_at: Optional[str]
    head_sha: str
    head_branch: str
    base_branch: str
    head_repo: str = ""
    author_login: str = ""
    mergeable: str = ""
    merge_state_status: str = ""

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN" and not self.merged_at

    @property
    def has_merge_conflicts(self) -> bool:
        return (
            self.mergeable.upper() == "CONFLICTING"
            or self.merge_state_status.upper() == "DIRTY"
        )

    @property
    def mergeability_is_known(self) -> bool:
        return any(
            value.upper() not in {"", "UNKNOWN"}
            for value in (self.mergeable, self.merge_state_status)
        )


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    state: str
    bucket: str
    workflow: str
    link: str
    started_at: str
    completed_at: str
    description: str

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "Check":
        return cls(
            name=str(value.get("name") or ""),
            state=str(value.get("state") or "").upper(),
            bucket=str(value.get("bucket") or "").lower(),
            workflow=str(value.get("workflow") or ""),
            link=str(value.get("link") or ""),
            started_at=str(value.get("startedAt") or ""),
            completed_at=str(value.get("completedAt") or ""),
            description=str(value.get("description") or ""),
        )

    @property
    def is_failure(self) -> bool:
        return self.bucket in FAILURE_BUCKETS or self.state in FAILURE_STATES

    @property
    def is_complete(self) -> bool:
        return self.is_failure or self.is_pass

    @property
    def is_pass(self) -> bool:
        return self.bucket in PASS_BUCKETS or self.state in PASS_STATES

    def failure_key(self, head_sha: str) -> str:
        """Return a stable identity for one failed check execution."""

        identity = {
            "version": FAILURE_KEY_VERSION,
            "head_sha": head_sha,
            "name": self.name,
            "workflow": self.workflow,
            "link": self.link,
            "started_at": self.started_at,
            "completed_at": self.completed_at if not self.started_at else "",
        }
        serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class CheckSnapshot:
    head_sha: str
    checks_reported: bool
    checks: tuple[Check, ...]


@dataclasses.dataclass(frozen=True)
class StartupStatus:
    checks_reported: bool
    total_checks: int
    passed_checks: int
    failed_checks: int
    pending_checks: int
    has_merge_conflicts: bool
    mergeability_is_known: bool
    check_snapshot: Optional[CheckSnapshot] = None

    @property
    def ci_is_green(self) -> bool:
        return (
            self.checks_reported
            and self.total_checks > 0
            and self.failed_checks == 0
            and self.pending_checks == 0
        )

    @property
    def should_synchronize(self) -> bool:
        return self.failed_checks > 0 or self.has_merge_conflicts


@dataclasses.dataclass(frozen=True)
class Review:
    id: str
    author_login: str
    state: str
    body: str
    submitted_at: str
    commit_sha: str
    url: str = ""

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "Review":
        author = value.get("author") or {}
        if isinstance(author, Mapping):
            author_login = str(author.get("login") or "")
        else:
            author_login = str(author or "")

        commit = value.get("commit") or {}
        if isinstance(commit, Mapping):
            commit_sha = str(commit.get("oid") or commit.get("sha") or "")
        else:
            commit_sha = str(commit or "")

        return cls(
            id=str(value.get("id") or ""),
            author_login=author_login,
            state=str(value.get("state") or "").upper(),
            body=str(value.get("body") or ""),
            submitted_at=str(value.get("submittedAt") or ""),
            commit_sha=commit_sha,
            url=str(value.get("url") or value.get("htmlUrl") or ""),
        )

    @property
    def is_submitted(self) -> bool:
        return bool(self.submitted_at) or self.state in SUBMITTED_REVIEW_STATES

    def is_from_other(self, pull_request: PullRequest) -> bool:
        if not self.author_login:
            return False
        if not pull_request.author_login:
            return True
        return self.author_login.casefold() != pull_request.author_login.casefold()

    def review_key(self) -> str:
        """Return a stable identity for one submitted review event."""

        identity = {
            "version": REVIEW_KEY_VERSION,
            "id": self.id,
            "author_login": self.author_login,
            "state": self.state,
            "body": self.body,
            "submitted_at": self.submitted_at,
            "commit_sha": self.commit_sha,
        }
        serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class ReviewComment:
    """One comment in an inline pull-request review thread."""

    id: str
    author_login: str
    body: str
    created_at: str = ""
    updated_at: str = ""
    commit_sha: str = ""
    path: str = ""
    line: Optional[int] = None
    original_line: Optional[int] = None
    diff_hunk: str = ""
    url: str = ""
    reply_to_id: str = ""

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "ReviewComment":
        author = value.get("author") or {}
        if isinstance(author, Mapping):
            author_login = str(author.get("login") or "")
        else:
            author_login = str(author or "")

        commit = value.get("commit") or {}
        if isinstance(commit, Mapping):
            commit_sha = str(commit.get("oid") or commit.get("sha") or "")
        else:
            commit_sha = str(commit or "")

        reply_to = value.get("replyTo") or {}
        if isinstance(reply_to, Mapping):
            reply_to_id = str(reply_to.get("id") or "")
        else:
            reply_to_id = str(reply_to or "")

        return cls(
            id=str(value.get("id") or ""),
            author_login=author_login,
            body=str(value.get("body") or ""),
            created_at=str(value.get("createdAt") or ""),
            updated_at=str(value.get("updatedAt") or ""),
            commit_sha=commit_sha,
            path=str(value.get("path") or ""),
            line=_optional_int(value.get("line")),
            original_line=_optional_int(value.get("originalLine")),
            diff_hunk=str(value.get("diffHunk") or ""),
            url=str(value.get("url") or value.get("htmlUrl") or ""),
            reply_to_id=reply_to_id,
        )


@dataclasses.dataclass(frozen=True)
class ReviewThread:
    """An inline pull-request review thread and its comment history."""

    id: str
    is_resolved: bool
    comments: tuple[ReviewComment, ...] = ()
    path: str = ""
    line: Optional[int] = None
    original_line: Optional[int] = None
    is_outdated: bool = False
    author_login: str = ""

    def __post_init__(self) -> None:
        if not self.author_login and self.comments:
            object.__setattr__(
                self,
                "author_login",
                self.comments[0].author_login,
            )

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "ReviewThread":
        comments_value = value.get("comments") or {}
        if isinstance(comments_value, Mapping):
            comment_values = comments_value.get("nodes") or []
        elif isinstance(comments_value, list):
            comment_values = comments_value
        else:
            comment_values = []
        if not isinstance(comment_values, list):
            raise TypeError(f"Unexpected review comment data: {comments_value!r}")

        return cls(
            id=str(value.get("id") or ""),
            is_resolved=_as_bool(value.get("isResolved")),
            comments=tuple(
                ReviewComment.from_json(comment)
                for comment in comment_values
                if isinstance(comment, Mapping)
            ),
            path=str(value.get("path") or ""),
            line=_optional_int(value.get("line")),
            original_line=_optional_int(value.get("originalLine")),
            is_outdated=_as_bool(value.get("isOutdated")),
            author_login=str(value.get("authorLogin") or ""),
        )

    @property
    def is_unresolved(self) -> bool:
        return not self.is_resolved

    @property
    def latest_comment(self) -> Optional[ReviewComment]:
        return self.comments[-1] if self.comments else None

    def is_from_other(self, pull_request: PullRequest) -> bool:
        authors = [
            author
            for author in [self.author_login]
            + [comment.author_login for comment in self.comments]
            if author
        ]
        if not authors:
            return False
        if not pull_request.author_login:
            return True
        pull_request_author = pull_request.author_login.casefold()
        return any(author.casefold() != pull_request_author for author in authors)

    def review_thread_key(self) -> str:
        """Return a stable identity for one thread comment history."""

        identity = {
            "version": REVIEW_THREAD_KEY_VERSION,
            "id": self.id,
            "comments": [
                {
                    "id": comment.id,
                    "author_login": comment.author_login,
                    "body": comment.body,
                    "created_at": comment.created_at,
                    "updated_at": comment.updated_at,
                    "reply_to_id": comment.reply_to_id,
                }
                for comment in self.comments
            ],
        }
        serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def thread_key(self) -> str:
        """Alias for callers that use the GitHub review-thread terminology."""

        return self.review_thread_key()


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.casefold() not in {"", "0", "false", "no", "null"}
    return bool(value)
