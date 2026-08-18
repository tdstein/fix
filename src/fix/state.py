from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping
from urllib.parse import quote

from .constants import STATE_VERSION
from .errors import MonitorError


def timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_state_path(repo: str, number: int) -> Path:
    cache_root = (
        os.environ.get("XDG_STATE_HOME")
        or os.environ.get("XDG_CACHE_HOME")
        or str(Path.home() / ".cache")
    )
    repo_slug = quote(repo, safe="")
    return Path(cache_root) / "fix" / f"{repo_slug}-pr-{number}.json"


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": STATE_VERSION,
                "seen_failures": {},
                "seen_reviews": {},
                "seen_comments": {},
                "agent_attempts_by_head": {},
            }

        try:
            value = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise MonitorError(
                f"Could not read state file {self.path}: {error}."
            ) from error

        if not isinstance(value, dict):
            raise MonitorError(f"State file must contain a JSON object: {self.path}.")
        if value.get("version") != STATE_VERSION:
            raise MonitorError(
                f"Unsupported state file version in {self.path}: "
                f"{value.get('version')!r}."
            )
        return value

    def save(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=str(self.path.parent),
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(value, file, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


@contextlib.contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """Prevent two monitor processes from launching agents concurrently."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MonitorError(
                f"Another fix process already holds {lock_path}."
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
