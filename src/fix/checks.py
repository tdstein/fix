from __future__ import annotations

from typing import Sequence

from .constants import LOGGER
from .errors import ChecksNotReportedError
from .github import GitHubClient
from .models import Check, CheckSnapshot, PullRequest, StartupStatus


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
    details = (
        f"{len(checks)} total · "
        f"{passed_checks} passed · "
        f"{failed_checks} failed · "
        f"{pending_checks} pending"
    )
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
    return f"CI ......... {state:<4} ({details})"


def inspect_startup(
    *,
    github: GitHubClient,
    pull_request: PullRequest,
) -> StartupStatus:
    LOGGER.info("Startup checks (2)")
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

    LOGGER.info(
        "  [1/2] %s",
        format_ci_check(checks, checks_reported=checks_reported),
    )

    base_branch = pull_request.base_branch or "(unknown base)"
    if startup_status.has_merge_conflicts:
        merge_state = "fail"
        merge_details = f"conflicts vs {base_branch}"
    elif not startup_status.mergeability_is_known:
        merge_state = "wait"
        merge_details = f"still calculating vs {base_branch}"
    else:
        merge_state = "pass"
        merge_details = f"no conflicts vs {base_branch}"
    LOGGER.info(
        "  [2/2] Mergeable .. %-4s (%s)",
        merge_state,
        merge_details,
    )
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
            "Startup requires action -> synchronizing (%s).",
            " and ".join(reasons),
        )
    elif status.ci_is_green and status.mergeability_is_known:
        LOGGER.info(
            "Startup passed -> entering monitor.",
        )
    else:
        LOGGER.info(
            "Startup pending -> entering monitor while GitHub reports back.",
        )
