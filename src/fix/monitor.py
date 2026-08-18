from __future__ import annotations

import textwrap
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .agents import (
    build_agent_prompt,
    build_review_comment_prompt,
    build_review_prompt,
    synchronize_with_conflict_resolution,
)
from .checks import (
    fetch_review_threads,
    find_new_review_threads,
    find_new_reviews,
    format_ci_check,
)
from .constants import (
    DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD,
    LOGGER,
)
from .errors import ChecksNotReportedError, MonitorError
from .github import CommandRunner, GitHubClient
from .models import Check, CheckSnapshot, PullRequest, Review, ReviewThread
from .repository import validate_agent_checkout
from .state import StateStore, timestamp


class Monitor:
    def __init__(
        self,
        *,
        github: GitHubClient,
        target: str,
        workdir: Path,
        state_store: StateStore,
        agent_launcher: Any,
        runner: Optional[CommandRunner] = None,
        initial_check_snapshot: Optional[CheckSnapshot] = None,
        synchronize_with_conflict_resolution_fn: Callable[..., PullRequest] = (
            synchronize_with_conflict_resolution
        ),
        validate_agent_checkout_fn: Callable[..., None] = validate_agent_checkout,
    ) -> None:
        self.github = github
        self.target = target
        self.workdir = workdir
        self.state_store = state_store
        self.agent_launcher = agent_launcher
        self.runner = runner or github.runner
        self.poll_again_immediately = False
        self.poll_count = 0
        self.agents_launched = 0
        self.last_poll_at: Optional[float] = None
        self.last_status = "starting"
        self.last_pull_request: Optional[PullRequest] = None
        self.stop_reason: Optional[str] = None
        self._last_check_line: Optional[str] = None
        self._initial_check_snapshot = initial_check_snapshot
        self._synchronize_with_conflict_resolution = (
            synchronize_with_conflict_resolution_fn
        )
        self._validate_agent_checkout = validate_agent_checkout_fn

    def _set_status(self, status: str, message: Optional[str] = None) -> None:
        changed = status != self.last_status
        self.last_status = status
        if changed and message:
            LOGGER.info("%s", message)

    def _synchronize_conflicts(self, pull_request: PullRequest) -> bool:
        self._set_status("merge conflicts")
        LOGGER.info(
            "➜ conflict sync · PR #%d · base `%s`",
            pull_request.number,
            pull_request.base_branch or "(unknown)",
        )
        synchronized_pull_request = self._synchronize_with_conflict_resolution(
            runner=self.runner,
            github=self.github,
            workdir=self.workdir,
            pull_request=pull_request,
            state_path=self.state_store.path,
            agent_launcher=self.agent_launcher,
        )
        if synchronized_pull_request.head_sha != pull_request.head_sha:
            LOGGER.info(
                "… conflict sync advanced PR #%d to %s; waiting for CI",
                pull_request.number,
                synchronized_pull_request.head_sha[:12],
            )
        else:
            LOGGER.info(
                "… conflict sync completed without changing the PR head"
            )
        return False

    def _log_agent_launch(
        self,
        *,
        agent_kind: str,
        details: str,
        log_path: Path,
    ) -> None:
        LOGGER.info("━" * 72)
        LOGGER.info("➜ Agent %s · %s", agent_kind, details)
        LOGGER.info("  session: %s", log_path)

    def _log_agent_result(self, *, agent_kind: str, returncode: int) -> None:
        elapsed = getattr(self.agent_launcher, "last_elapsed_seconds", None)
        elapsed_suffix = (
            f" · {elapsed:.0f}s" if isinstance(elapsed, (int, float)) else ""
        )
        if returncode == 0:
            LOGGER.info("✓ Agent %s completed%s", agent_kind, elapsed_suffix)
        else:
            LOGGER.error(
                "Agent %s failed with exit code %d%s",
                agent_kind,
                returncode,
                elapsed_suffix,
            )

    def _verify_agent_result(
        self,
        *,
        pull_request: PullRequest,
        state: dict[str, Any],
        agent_kind: str,
    ) -> Optional[PullRequest]:
        try:
            updated_pull_request = self.github.get_pull_request(self.target)
        except MonitorError as error:
            self.poll_again_immediately = False
            self.state_store.save(state)
            LOGGER.warning(
                "Could not verify the PR head after the %s agent: %s",
                agent_kind,
                error,
            )
            return None

        self.last_pull_request = updated_pull_request
        if updated_pull_request.head_sha == pull_request.head_sha:
            LOGGER.info(
                "✓ %s agent · remote head unchanged",
                agent_kind,
            )
        else:
            LOGGER.info(
                "✓ %s agent · head advanced %s -> %s",
                agent_kind,
                pull_request.head_sha[:12],
                updated_pull_request.head_sha[:12],
            )

        try:
            self._validate_agent_checkout(
                runner=self.runner,
                workdir=self.workdir,
                pull_request=updated_pull_request,
            )
        except MonitorError as error:
            self.poll_again_immediately = False
            self.state_store.save(state)
            LOGGER.error(
                "The %s agent did not leave a clean checkout matching the "
                "remote PR head: %s",
                agent_kind,
                error,
            )
            return None
        return updated_pull_request

    def poll_once(self) -> bool:
        self.poll_again_immediately = False
        self.stop_reason = None
        self.poll_count += 1
        self.last_poll_at = time.time()
        pull_request = self.github.get_pull_request(self.target)
        self.last_pull_request = pull_request
        state = self.state_store.load()

        if not pull_request.is_open:
            self.state_store.save(state)
            self.stop_reason = (
                f"PR #{pull_request.number} is "
                f"{'merged' if pull_request.merged_at else pull_request.state.lower()}."
            )
            self._set_status("closed")
            return True

        if pull_request.has_merge_conflicts:
            self._initial_check_snapshot = None
            return self._synchronize_conflicts(pull_request)

        initial_check_snapshot = self._initial_check_snapshot
        if (
            initial_check_snapshot is not None
            and initial_check_snapshot.head_sha == pull_request.head_sha
        ):
            self._initial_check_snapshot = None
            checks = initial_check_snapshot.checks
            checks_reported = initial_check_snapshot.checks_reported
            log_check_status = False
        else:
            self._initial_check_snapshot = None
            checks_reported = True
            try:
                checks = self.github.get_checks(pull_request)
            except ChecksNotReportedError:
                checks = []
                checks_reported = False
            log_check_status = True
        failures = [check for check in checks if check.is_failure]
        check_line = format_ci_check(
            checks,
            checks_reported=checks_reported,
        )
        if log_check_status:
            if check_line != self._last_check_line:
                LOGGER.info(
                    "%s · #%d @ %s",
                    check_line,
                    pull_request.number,
                    pull_request.head_sha[:8],
                )
        self._last_check_line = check_line

        if not failures:
            review_threads = fetch_review_threads(
                github=self.github,
                pull_request=pull_request,
            )
            seen_comments = state.setdefault("seen_comments", {})
            new_comments = find_new_review_threads(
                threads=review_threads,
                pull_request=pull_request,
                seen_threads=seen_comments,
            )

            if new_comments:
                self._set_status("unresolved comments")
                return self._launch_comment_agent(
                    pull_request=pull_request,
                    new_comments=new_comments,
                    state=state,
                    seen_comments=seen_comments,
                )

            reviews = self.github.get_reviews(pull_request)
            seen_reviews = state.setdefault("seen_reviews", {})
            new_reviews = find_new_reviews(
                reviews=reviews,
                pull_request=pull_request,
                seen_reviews=seen_reviews,
            )

            if new_reviews:
                self._set_status("review feedback")
                return self._launch_review_agent(
                    pull_request=pull_request,
                    new_reviews=new_reviews,
                    state=state,
                    seen_reviews=seen_reviews,
                )

            if checks_reported and all(check.is_complete for check in checks):
                self.state_store.save(state)
                self.stop_reason = (
                    f"CI is complete with {len(checks)} "
                    f"{'check' if len(checks) == 1 else 'checks'} "
                    "and no new reviews."
                )
                self._set_status("all green")
                return True

            self.state_store.save(state)
            waiting_status = (
                "CI pending" if not checks_reported or not checks else "no new reviews"
            )
            waiting_message = (
                "… CI pending · checks not reported yet"
                if not checks_reported
                else "… CI pending · waiting for checks"
            )
            self._set_status(waiting_status, waiting_message)
            return False

        seen_failures = state.setdefault("seen_failures", {})
        new_failures = [
            (check.failure_key(pull_request.head_sha), check)
            for check in failures
            if check.failure_key(pull_request.head_sha) not in seen_failures
        ]

        if not new_failures:
            self.state_store.save(state)
            self._set_status(
                "failures already handled",
                (
                    "… failed checks already handled · "
                    f"{len(failures)} failure"
                    f"{'' if len(failures) == 1 else 's'}"
                ),
            )
            return False

        attempts_by_head = state.setdefault("agent_attempts_by_head", {})
        attempts = int(attempts_by_head.get(pull_request.head_sha, 0))
        if (
            DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD > 0
            and attempts >= DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD
        ):
            self.state_store.save(state)
            self._set_status("repair limit reached")
            LOGGER.warning(
                "Skipping repair for head %s: reached %d attempts.",
                pull_request.head_sha[:12],
                DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD,
            )
            return False

        try:
            self._validate_agent_checkout(
                runner=self.runner,
                workdir=self.workdir,
                pull_request=pull_request,
            )
        except MonitorError as error:
            self.state_store.save(state)
            LOGGER.error("%s", error)
            return False

        log_path = (
            self.state_store.path.parent
            / "logs"
            / f"{timestamp().replace(':', '').replace('+00:00', 'Z')}"
            f"-{pull_request.head_sha[:12]}-attempt-{attempts + 1}.log"
        )
        prompt = build_agent_prompt(pull_request, new_failures, workdir=self.workdir)
        self.agents_launched += 1
        failure_names = ", ".join(check.name for _, check in new_failures)
        self._log_agent_launch(
            agent_kind="repair",
            details=(
                f"{len(new_failures)} new failure"
                f"{'' if len(new_failures) == 1 else 's'} · {failure_names}"
            ),
            log_path=log_path,
        )
        returncode = self.agent_launcher.launch(prompt, log_path)
        self.poll_again_immediately = True

        if (
            self._verify_agent_result(
                pull_request=pull_request,
                state=state,
                agent_kind="repair",
            )
            is None
        ):
            return False

        attempts_by_head[pull_request.head_sha] = attempts + 1
        for key, _ in new_failures:
            seen_failures[key] = {"seen_at": timestamp()}
        self._prune_seen_items(seen_failures)
        self.state_store.save(state)
        self._log_agent_result(agent_kind="repair", returncode=returncode)
        return False

    def _launch_comment_agent(
        self,
        *,
        pull_request: PullRequest,
        new_comments: Sequence[tuple[str, ReviewThread]],
        state: dict[str, Any],
        seen_comments: dict[str, Any],
    ) -> bool:
        try:
            self._validate_agent_checkout(
                runner=self.runner,
                workdir=self.workdir,
                pull_request=pull_request,
            )
        except MonitorError as error:
            self.state_store.save(state)
            LOGGER.error("%s", error)
            return False

        log_path = (
            self.state_store.path.parent
            / "logs"
            / f"{timestamp().replace(':', '').replace('+00:00', 'Z')}"
            f"-{pull_request.head_sha[:12]}-comments-{len(seen_comments) + 1}.log"
        )
        prompt = build_review_comment_prompt(
            pull_request,
            new_comments,
            workdir=self.workdir,
        )
        self.agents_launched += 1
        self._log_agent_launch(
            agent_kind="comment",
            details=(
                f"{len(new_comments)} unresolved comment"
                f"{'' if len(new_comments) == 1 else 's'}"
            ),
            log_path=log_path,
        )
        for _, thread in new_comments:
            location = thread.path or "unknown file"
            if thread.line is not None:
                location += f":{thread.line}"
            latest_comment = thread.latest_comment
            body = textwrap.shorten(
                " ".join((latest_comment.body if latest_comment else "").split())
                or "(no comment body)",
                width=180,
                placeholder="...",
            )
            LOGGER.info(
                "  comment @%s %s: %s",
                thread.author_login or "unknown",
                location,
                body,
            )
        returncode = self.agent_launcher.launch(prompt, log_path)
        self.poll_again_immediately = True

        if (
            self._verify_agent_result(
                pull_request=pull_request,
                state=state,
                agent_kind="comment",
            )
            is None
        ):
            return False

        for key, _ in new_comments:
            seen_comments[key] = {"seen_at": timestamp()}
        self._prune_seen_items(seen_comments)
        self.state_store.save(state)
        self._log_agent_result(agent_kind="comment", returncode=returncode)
        return False

    def _launch_review_agent(
        self,
        *,
        pull_request: PullRequest,
        new_reviews: Sequence[tuple[str, Review]],
        state: dict[str, Any],
        seen_reviews: dict[str, Any],
    ) -> bool:
        try:
            self._validate_agent_checkout(
                runner=self.runner,
                workdir=self.workdir,
                pull_request=pull_request,
            )
        except MonitorError as error:
            self.state_store.save(state)
            LOGGER.error("%s", error)
            return False

        log_path = (
            self.state_store.path.parent
            / "logs"
            / f"{timestamp().replace(':', '').replace('+00:00', 'Z')}"
            f"-{pull_request.head_sha[:12]}-review-{len(seen_reviews) + 1}.log"
        )
        prompt = build_review_prompt(pull_request, new_reviews, workdir=self.workdir)
        self.agents_launched += 1
        self._log_agent_launch(
            agent_kind="review",
            details=(
                f"{len(new_reviews)} new review"
                f"{'' if len(new_reviews) == 1 else 's'}"
            ),
            log_path=log_path,
        )
        for _, review in new_reviews:
            body = textwrap.shorten(
                " ".join(review.body.split()) or "(no comment body)",
                width=180,
                placeholder="...",
            )
            LOGGER.info(
                "  review @%s: %s",
                review.author_login or "unknown",
                body,
            )
        returncode = self.agent_launcher.launch(prompt, log_path)
        self.poll_again_immediately = True

        if (
            self._verify_agent_result(
                pull_request=pull_request,
                state=state,
                agent_kind="review",
            )
            is None
        ):
            return False

        for key, _ in new_reviews:
            seen_reviews[key] = {"seen_at": timestamp()}
        self._prune_seen_items(seen_reviews)
        self.state_store.save(state)
        self._log_agent_result(agent_kind="review", returncode=returncode)
        return False

    @staticmethod
    def _prune_seen_items(items: dict[str, Any], limit: int = 500) -> None:
        if len(items) <= limit:
            return
        ordered = sorted(
            items.items(),
            key=lambda item: str(item[1].get("seen_at") or ""),
        )
        for key, _ in ordered[:-limit]:
            del items[key]
