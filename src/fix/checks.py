from __future__ import annotations

from typing import Any, Mapping, Sequence

from .constants import LOGGER
from .errors import ChecksNotReportedError
from .github import GitHubClient
from .models import Check, CheckSnapshot, PullRequest, Review, StartupStatus


STATUS_GLYPHS = {
    "pass": "✓",
    "fail": "✗",
    "wait": "…",
}


def summarize_checks(
    checks: Sequence[Check],
    *,
    checks_reported: bool,
) -> tuple[str, str]:
    if not checks_reported:
        return "wait", "checks not reported yet"

    passed_checks = sum(check.is_pass for check in checks)
    failed_checks = sum(check.is_failure for check in checks)
    pending_checks = sum(not check.is_complete for check in checks)
    if failed_checks:
        state = "fail"
    elif checks and pending_checks == 0:
        state = "pass"
    else:
        state = "wait"
    if failed_checks:
        details = f"{passed_checks}/{len(checks)} passed"
        details += f" · {failed_checks} failed"
        if pending_checks:
            details += f" · {pending_checks} pending"
    elif pending_checks:
        details = f"{len(checks) - pending_checks}/{len(checks)} complete"
        details += f" · {pending_checks} pending"
    else:
        details = f"{passed_checks}/{len(checks)} passed"
    return state, details


def format_ci_check(
    checks: Sequence[Check],
    *,
    checks_reported: bool,
) -> str:
    state, details = summarize_checks(
        checks,
        checks_reported=checks_reported,
    )
    return f"{STATUS_GLYPHS[state]} CI          {details}"


def format_mergeability(
    pull_request: PullRequest,
) -> str:
    base_branch = pull_request.base_branch or "(unknown base)"
    if pull_request.has_merge_conflicts:
        state = "fail"
        details = f"conflicts vs {base_branch}"
    elif not pull_request.mergeability_is_known:
        state = "wait"
        details = f"still calculating vs {base_branch}"
    else:
        state = "pass"
        details = f"no conflicts vs {base_branch}"
    return f"{STATUS_GLYPHS[state]} Mergeable   {details}"


def find_new_reviews(
    *,
    reviews: Sequence[Review],
    pull_request: PullRequest,
    seen_reviews: Mapping[str, Any],
) -> list[tuple[str, Review]]:
    other_reviews = [
        review
        for review in reviews
        if review.is_submitted and review.is_from_other(pull_request)
    ]
    return [
        (review.review_key(), review)
        for review in other_reviews
        if review.review_key() not in seen_reviews
    ]


def inspect_startup(
    *,
    github: GitHubClient,
    pull_request: PullRequest,
) -> StartupStatus:
    LOGGER.info("Startup checks")
    try:
        checks = tuple(github.get_checks(pull_request))
    except ChecksNotReportedError:
        checks = ()
        checks_reported = False
    else:
        checks_reported = True

    passed_checks = sum(check.is_pass for check in checks)
    failed_checks = sum(check.is_failure for check in checks)
    pending_checks = sum(not check.is_complete for check in checks)
    startup_status = StartupStatus(
        checks_reported=checks_reported,
        total_checks=len(checks),
        passed_checks=passed_checks,
        failed_checks=failed_checks,
        pending_checks=pending_checks,
        has_merge_conflicts=pull_request.has_merge_conflicts,
        mergeability_is_known=pull_request.mergeability_is_known,
        check_snapshot=CheckSnapshot(
            head_sha=pull_request.head_sha,
            checks_reported=checks_reported,
            checks=checks,
        ),
    )

    LOGGER.info("%s", format_ci_check(checks, checks_reported=checks_reported))
    LOGGER.info("%s", format_mergeability(pull_request))
    return startup_status


def should_synchronize_pull_request(
    *,
    github: GitHubClient,
    pull_request: PullRequest,
) -> bool:
    return inspect_startup(
        github=github,
        pull_request=pull_request,
    ).should_synchronize


def log_startup_decision(status: StartupStatus) -> None:
    if status.should_synchronize:
        reasons = []
        if status.failed_checks:
            reasons.append("CI failures")
        if status.has_merge_conflicts:
            reasons.append("merge conflicts")
        LOGGER.info(
            "➜ synchronizing · %s",
            " and ".join(reasons),
        )
    elif status.ci_is_green and status.mergeability_is_known:
        LOGGER.info(
            "⟳ monitoring · waiting for review changes",
        )
    else:
        LOGGER.info(
            "… waiting for CI or mergeability",
        )
