from __future__ import annotations

import shlex
from typing import Sequence


class MonitorError(RuntimeError):
    """An expected failure while resolving or monitoring the target."""


class CommandError(MonitorError):
    """A subprocess returned an unexpected result."""

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stderr: str = "",
    ) -> None:
        self.command = list(command)
        self.returncode = returncode
        self.stderr = stderr.strip()
        detail = shlex.join(command)
        if self.stderr:
            detail += ": " + self.stderr
        super().__init__(f"Command failed with exit code {returncode}: {detail}")


class ChecksNotReportedError(CommandError):
    """GitHub has not reported checks for the current pull request head yet."""
