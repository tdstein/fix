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
