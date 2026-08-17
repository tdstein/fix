from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .agents import (
    build_agent_prompt,
    build_review_prompt,
    synchronize_with_conflict_resolution,
)
from .checks import format_ci_check
from .constants import (
    DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD,
    LOGGER,
)
from .errors import ChecksNotReportedError, MonitorError
from .github import CommandRunner, GitHubClient
from .models import Check, CheckSnapshot, PullRequest, Review
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
        self.stop_reason: Optional[str] = None
        self._initial_check_snapshot = initial_check_snapshot
        self._synchronize_with_conflict_resolution = (
            synchronize_with_conflict_resolution_fn
        )
        self._validate_agent_checkout = validate_agent_checkout_fn

    def _synchronize_conflicts(self, pull_request: PullRequest) -> bool:
        LOGGER.info(
            "Monitoring found merge conflicts for PR #%d; synchronizing with "
            "base branch `%s`.",
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
                "Conflict synchronization advanced PR #%d to %s. "
                "Waiting for GitHub to recognize the new head and start CI.",
                pull_request.number,
                synchronized_pull_request.head_sha[:12],
            )
        else:
            LOGGER.info(
                "Conflict synchronization completed without changing the PR head."
            )
        return False

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

        if updated_pull_request.head_sha == pull_request.head_sha:
            LOGGER.info(
                "The %s agent finished without a visible remote head change.",
                agent_kind,
            )
        else:
            LOGGER.info(
                "The PR head advanced from %s to %s.",
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
        pull_request = self.github.get_pull_request(self.target)
        state = self.state_store.load()

        if not pull_request.is_open:
            self.state_store.save(state)
            self.stop_reason = (
                f"PR #{pull_request.number} is "
                f"{'merged' if pull_request.merged_at else pull_request.state.lower()}."
            )
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
        if log_check_status:
            LOGGER.info(
                "%s PR #%s at head %s: %s.",
                "Checking" if self.poll_count == 1 else "Polling",
                pull_request.number,
                pull_request.head_sha[:12],
                format_ci_check(checks, checks_reported=checks_reported),
            )

        if not failures:
            reviews = self.github.get_reviews(pull_request)
            other_reviews = [
                review
                for review in reviews
                if review.is_submitted and review.is_from_other(pull_request)
            ]
            seen_reviews = state.setdefault("seen_reviews", {})
            new_reviews = [
                (review.review_key(), review)
                for review in other_reviews
                if review.review_key() not in seen_reviews
            ]

            if new_reviews:
                return self._launch_review_agent(
                    pull_request=pull_request,
                    new_reviews=new_reviews,
                    state=state,
                    seen_reviews=seen_reviews,
                )

            if checks_reported and all(check.is_complete for check in checks):
                self.state_store.save(state)
                self.stop_reason = (
                    f"CI is complete with {len(checks)} checks "
                    "and no new reviews."
                )
                return True

            self.state_store.save(state)
            LOGGER.info(
                "CI is still waiting; %s.",
                (
                    "GitHub has not reported any checks yet"
                    if not checks_reported
                    else "no new reviews require a review agent"
                ),
            )
            return False

        seen_failures = state.setdefault("seen_failures", {})
        new_failures = [
            (check.failure_key(pull_request.head_sha), check)
            for check in failures
            if check.failure_key(pull_request.head_sha) not in seen_failures
        ]

        if not new_failures:
            self.state_store.save(state)
            LOGGER.info(
                "No new failed checks require a repair agent; %d failure%s already handled.",
                len(failures),
                "" if len(failures) == 1 else "s",
            )
            return False

        attempts_by_head = state.setdefault("agent_attempts_by_head", {})
        attempts = int(attempts_by_head.get(pull_request.head_sha, 0))
        if (
            DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD > 0
            and attempts >= DEFAULT_MAX_AGENT_ATTEMPTS_PER_HEAD
        ):
            self.state_store.save(state)
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
        LOGGER.info(
            "Launching interactive Codex for %d new failure%s. Session log: %s",
            len(new_failures),
            "" if len(new_failures) == 1 else "s",
            log_path,
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
        if returncode == 0:
            LOGGER.info("The repair agent completed successfully.")
        else:
            LOGGER.error("The repair agent failed with exit code %d.", returncode)
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
        LOGGER.info(
            "Launching interactive Codex for %d new review%s. Session log: %s",
            len(new_reviews),
            "" if len(new_reviews) == 1 else "s",
            log_path,
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
        if returncode == 0:
            LOGGER.info("The review agent completed successfully.")
        else:
            LOGGER.error("The review agent failed with exit code %d.", returncode)
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
