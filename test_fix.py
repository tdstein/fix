import dataclasses
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import fix
from fix import (
    AgentLauncher,
    Check,
    GitHubClient,
    Monitor,
    PullRequest,
    Review,
    StateStore,
    build_agent_command,
    build_agent_prompt,
    build_conflict_prompt,
    build_review_prompt,
    default_state_path,
    synchronize_pull_request,
)


class CheckTests(unittest.TestCase):
    def test_failure_key_is_stable_and_head_specific(self):
        check = Check(
            name="server amd64",
            state="FAILURE",
            bucket="fail",
            workflow="ci",
            link="https://github.com/example-org/example-repo/actions/runs/1",
            started_at="2026-08-14T12:00:00Z",
            completed_at="2026-08-14T12:10:00Z",
            description="compile failed",
        )
        self.assertEqual(check.failure_key("abc"), check.failure_key("abc"))
        self.assertNotEqual(check.failure_key("abc"), check.failure_key("def"))
        self.assertTrue(check.is_failure)
        self.assertTrue(check.is_complete)
        self.assertFalse(check.is_pass)

    def test_success_is_not_a_failure(self):
        check = Check(
            name="lint",
            state="SUCCESS",
            bucket="pass",
            workflow="ci",
            link="",
            started_at="",
            completed_at="",
            description="",
        )
        self.assertFalse(check.is_failure)
        self.assertTrue(check.is_complete)
        self.assertTrue(check.is_pass)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
            author_login="contributor",
        )

    def test_review_parses_github_fields_and_identifies_other_authors(self):
        review = Review.from_json(
            {
                "id": "review-1",
                "author": {"login": "maintainer"},
                "state": "CHANGES_REQUESTED",
                "body": "Please handle this edge case.",
                "submittedAt": "2026-08-14T12:00:00Z",
                "commit": {"oid": "abc123"},
                "url": "https://github.com/example-org/example-repo/pull/123#pullrequestreview-1",
            }
        )

        self.assertTrue(review.is_submitted)
        self.assertTrue(review.is_from_other(self.pull_request))
        self.assertEqual(review.author_login, "maintainer")
        self.assertEqual(review.commit_sha, "abc123")
        self.assertEqual(review.review_key(), review.review_key())

    def test_review_from_pull_request_author_is_ignored(self):
        review = Review(
            id="review-1",
            author_login="CONTRIBUTOR",
            state="COMMENTED",
            body="Follow-up",
            submitted_at="2026-08-14T12:00:00Z",
            commit_sha="abc123",
        )

        self.assertFalse(review.is_from_other(self.pull_request))


class StateStoreTests(unittest.TestCase):
    def test_default_path_uses_fix_namespace(self):
        self.assertEqual(
            default_state_path("example-org/example-repo", 123).parts[-2:],
            ("fix", "example-org-example-repo-pr-123.json"),
        )

    def test_save_and_load_is_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            value = store.load()
            value["seen_failures"]["key"] = {"seen_at": "now"}
            store.save(value)
            self.assertEqual(store.load()["seen_failures"]["key"]["seen_at"], "now")
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertEqual(store.load()["seen_reviews"], {})


class RunTests(unittest.TestCase):
    def test_run_constructs_state_store_with_only_its_path(self):
        pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
        )
        github = mock.Mock()
        github.get_pull_request.return_value = pull_request
        monitor = mock.Mock()
        monitor.poll_once.return_value = True
        lock = mock.MagicMock()
        lock.__enter__.return_value = lock

        with mock.patch("fix.configure_logging"), \
            mock.patch("fix.GitHubClient", return_value=github), \
            mock.patch("fix.default_state_path", return_value=Path("/tmp/state.json")), \
            mock.patch("fix.StateStore") as state_store, \
            mock.patch("fix.AgentLauncher"), \
            mock.patch("fix.Monitor", return_value=monitor), \
            mock.patch("fix.synchronize_pull_request", return_value=pull_request), \
            mock.patch("fix.state_lock", return_value=lock):
            self.assertEqual(fix.run(), 0)

        state_store.assert_called_once_with(Path("/tmp/state.json"))

    def test_run_launches_conflict_agent_and_retries_synchronization(self):
        initial_pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
        )
        updated_pull_request = dataclasses.replace(
            initial_pull_request,
            head_sha="def456",
        )
        github = mock.Mock()
        github.get_pull_request.side_effect = [
            initial_pull_request,
            updated_pull_request,
        ]
        monitor = mock.Mock()
        monitor.poll_once.return_value = True
        lock = mock.MagicMock()
        lock.__enter__.return_value = lock
        conflict = fix.CommandError(
            ["gh", "pr", "update-branch", "123"],
            1,
            "X Cannot update PR branch due to conflicts",
        )

        with mock.patch("fix.configure_logging"), \
            mock.patch("fix.GitHubClient", return_value=github), \
            mock.patch("fix.default_state_path", return_value=Path("/tmp/state.json")), \
            mock.patch("fix.StateStore") as state_store, \
            mock.patch("fix.AgentLauncher") as agent_launcher_class, \
            mock.patch("fix.Monitor", return_value=monitor), \
            mock.patch(
                "fix.synchronize_pull_request",
                side_effect=[conflict, updated_pull_request],
            ) as synchronize, \
            mock.patch("fix.state_lock", return_value=lock):
            agent_launcher_class.return_value.launch.return_value = 0
            self.assertEqual(fix.run(), 0)

        self.assertEqual(synchronize.call_count, 2)
        self.assertEqual(
            synchronize.call_args_list[1].kwargs["pull_request"],
            updated_pull_request,
        )
        agent_launcher_class.return_value.launch.assert_called_once()
        self.assertIn(
            "conflict-resolution",
            agent_launcher_class.return_value.launch.call_args.args[0],
        )
        state_store.assert_called_once_with(Path("/tmp/state.json"))


class AgentCommandTests(unittest.TestCase):
    def test_command_is_fixed_to_codex_luna_and_max_effort(self):
        workdir = Path("/tmp/example-repo")
        command = build_agent_command(workdir=workdir, prompt="Fix the failure.")
        self.assertEqual(
            command,
            [
                "codex",
                "--model",
                "openai.gpt-5.6-luna",
                "--config",
                'model_reasoning_effort="max"',
                "--approve-for-me",
                "--strict-config",
                "--config",
                "sandbox_workspace_write.network_access=true",
                "--cd",
                str(workdir),
                "Fix the failure.",
            ],
        )


class AgentLauncherTests(unittest.TestCase):
    @mock.patch("fix.subprocess.Popen")
    def test_launches_codex_with_terminal_stdio(self, popen):
        process = popen.return_value
        process.wait.return_value = 0
        workdir = Path("/tmp/example-repo")

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "agent.log"
            result = AgentLauncher(workdir=workdir).launch(
                "Fix the failure.",
                log_path,
            )

        self.assertEqual(result, 0)
        popen.assert_called_once_with(
            build_agent_command(workdir=workdir, prompt="Fix the failure."),
            cwd=str(workdir),
        )
        process.wait.assert_called_once_with(timeout=2 * 60 * 60)


class PromptTests(unittest.TestCase):
    def test_prompt_contains_failure_context_and_push_constraints(self):
        pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="https://github.com/example-org/example-repo/pull/123",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
        )
        check = Check(
            name="server",
            state="FAILURE",
            bucket="fail",
            workflow="ci",
            link="https://github.com/example-org/example-repo/actions/runs/1",
            started_at="",
            completed_at="",
            description="compiler error",
        )
        prompt = build_agent_prompt(
            pull_request,
            [("failure-key", check)],
            workdir=Path("/tmp/example-repo"),
        )
        self.assertIn("compiler error", prompt)
        self.assertIn("Fix the CI failure", prompt)
        self.assertIn("failure-key", prompt)
        self.assertIn("fix-ci", prompt)
        self.assertIn("Never force-push", prompt)
        self.assertIn("exit Codex", prompt)
        self.assertIn("original `fix` polling loop", prompt)

    def test_conflict_prompt_instructs_agent_to_resolve_and_push_safely(self):
        pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
            head_repo="example-org/example-repo",
        )

        prompt = build_conflict_prompt(
            pull_request,
            workdir=Path("/tmp/example-repo"),
        )

        self.assertIn("conflict", prompt)
        self.assertIn("configured base branch", prompt)
        self.assertIn("Never force-push", prompt)
        self.assertIn("retry synchronization", prompt)


class ReviewPromptTests(unittest.TestCase):
    def test_prompt_explains_collaborative_review_behavior(self):
        pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="https://github.com/example-org/example-repo/pull/123",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
            author_login="contributor",
        )
        review = Review(
            id="review-1",
            author_login="maintainer",
            state="CHANGES_REQUESTED",
            body="Please handle this edge case.",
            submitted_at="2026-08-14T12:00:00Z",
            commit_sha="abc123",
        )

        prompt = build_review_prompt(
            pull_request,
            [("review-key", review)],
            workdir=Path("/tmp/example-repo"),
        )

        self.assertIn("Please handle this edge case.", prompt)
        self.assertIn("review-key", prompt)
        self.assertIn("Walk the user through", prompt)
        self.assertIn("clearly correct fixes", prompt)
        self.assertIn("subjective, ambiguous", prompt)
        self.assertIn("Keep this Codex session interactive", prompt)


class GitHubClientTests(unittest.TestCase):
    def test_initial_pull_request_lookup_uses_current_branch(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def run(self, command, *, cwd=None):
                self.calls.append((command, cwd))
                if command[:3] == ["gh", "repo", "view"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "example-org/example-repo\n",
                        "",
                    )
                if command[:3] == ["gh", "pr", "view"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "number": 123,
                                "title": "Example",
                                "url": "https://github.com/example-org/example-repo/pull/123",
                                "state": "OPEN",
                                "mergedAt": None,
                                "author": {"login": "contributor"},
                                "headRefOid": "abc123",
                                "headRefName": "fix-ci",
                                "baseRefName": "main",
                                "headRepository": {
                                    "nameWithOwner": "example-org/example-repo"
                                },
                            }
                        ),
                        "",
                    )
                raise AssertionError(f"unexpected command: {command}")

        runner = Runner()
        workdir = Path("/tmp/example-repo")
        pull_request = GitHubClient(cwd=workdir, runner=runner).get_pull_request()

        self.assertEqual(pull_request.number, 123)
        self.assertEqual(runner.calls[0][0][:3], ["gh", "repo", "view"])
        self.assertEqual(runner.calls[1][0][:3], ["gh", "pr", "view"])
        self.assertEqual(runner.calls[1][0][3], "--json")
        self.assertEqual(runner.calls[1][1], workdir)
        self.assertEqual(pull_request.author_login, "contributor")

    def test_reviews_are_loaded_from_pull_request_view(self):
        class Runner:
            def run(self, command, *, cwd=None):
                if command != ["gh", "pr", "view", "123", "--json", "reviews"]:
                    raise AssertionError(f"unexpected command: {command}")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "reviews": [
                                {
                                    "id": "review-1",
                                    "author": {"login": "maintainer"},
                                    "state": "APPROVED",
                                    "body": "Looks good.",
                                    "submittedAt": "2026-08-14T12:00:00Z",
                                    "commit": {"oid": "abc123"},
                                }
                            ]
                        }
                    ),
                    "",
                )

        pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
        )
        reviews = GitHubClient(
            cwd=Path("/tmp/example-repo"),
            runner=Runner(),
        ).get_reviews(pull_request)

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].body, "Looks good.")


class FakeRunner:
    def __init__(self, head_sha):
        self.head_sha = head_sha

    def run(self, command, *, cwd=None):
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, self.head_sha, "")
        if command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


class SynchronizeRunner:
    def __init__(
        self,
        head_sha,
        *,
        refreshed_head_sha=None,
        dirty="",
        update_returncode=0,
        update_stderr="",
    ):
        self.head_sha = head_sha
        self.refreshed_head_sha = refreshed_head_sha
        self.dirty = dirty
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.calls = []

    def run(self, command, *, cwd=None):
        self.calls.append((command, cwd))
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, self.head_sha, "")
        if command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, self.dirty, "")
        if command[:3] in (
            ["gh", "pr", "update-branch"],
            ["gh", "pr", "checkout"],
        ):
            if command[:3] == ["gh", "pr", "update-branch"]:
                return subprocess.CompletedProcess(
                    command,
                    self.update_returncode,
                    "",
                    self.update_stderr,
                )
            if command[:3] == ["gh", "pr", "checkout"]:
                self.head_sha = self.refreshed_head_sha or self.head_sha
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


class FakeGitHub:
    def __init__(self, pull_requests, checks, reviews=None):
        self.pull_requests = iter(pull_requests)
        self.last_pull_request = pull_requests[-1]
        self.checks = checks
        self.reviews = reviews or []
        self.review_calls = 0
        self.runner = FakeRunner(pull_requests[0].head_sha)

    def get_pull_request(self, target):
        try:
            self.last_pull_request = next(self.pull_requests)
        except StopIteration:
            pass
        self.runner.head_sha = self.last_pull_request.head_sha
        return self.last_pull_request

    def get_checks(self, pull_request):
        return self.checks

    def get_reviews(self, pull_request):
        self.review_calls += 1
        return self.reviews


class FakeAgent:
    def __init__(self):
        self.prompts = []

    def launch(self, prompt, log_path):
        self.prompts.append((prompt, log_path))
        return 0


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="https://github.com/example-org/example-repo/pull/123",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
        )
        self.failure = Check(
            name="server",
            state="FAILURE",
            bucket="fail",
            workflow="ci",
            link="https://github.com/example-org/example-repo/actions/runs/1",
            started_at="2026-08-14T12:00:00Z",
            completed_at="2026-08-14T12:10:00Z",
            description="compiler error",
        )

    def test_launches_once_for_a_persistent_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state_store = StateStore(Path(directory) / "state.json")
            github = FakeGitHub(
                [self.pull_request, self.pull_request],
                [self.failure],
            )
            agent = FakeAgent()
            monitor = Monitor(
                github=github,
                target="123",
                workdir=Path(directory),
                state_store=state_store,
                agent_launcher=agent,
                runner=github.runner,
            )
            monitor.poll_once()
            self.assertEqual(len(agent.prompts), 1)
            self.assertFalse(monitor.poll_once())
            self.assertEqual(len(agent.prompts), 1)

    def test_new_head_allows_a_new_repair_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            second_head = dataclasses.replace(self.pull_request, head_sha="def456")
            state_store = StateStore(Path(directory) / "state.json")
            github = FakeGitHub(
                [self.pull_request, second_head],
                [self.failure],
            )
            agent = FakeAgent()
            monitor = Monitor(
                github=github,
                target="123",
                workdir=Path(directory),
                state_store=state_store,
                agent_launcher=agent,
                runner=github.runner,
            )
            monitor.poll_once()
            monitor.poll_once()
            self.assertEqual(len(agent.prompts), 2)

    def test_passed_ci_launches_agent_for_a_new_review_from_another_author(self):
        review = Review(
            id="review-1",
            author_login="maintainer",
            state="CHANGES_REQUESTED",
            body="Please handle this edge case.",
            submitted_at="2026-08-14T12:00:00Z",
            commit_sha=self.pull_request.head_sha,
        )
        passed_check = Check(
            name="ci",
            state="SUCCESS",
            bucket="pass",
            workflow="ci",
            link="",
            started_at="2026-08-14T12:00:00Z",
            completed_at="2026-08-14T12:10:00Z",
            description="",
        )

        with tempfile.TemporaryDirectory() as directory:
            state_store = StateStore(Path(directory) / "state.json")
            github = FakeGitHub(
                [self.pull_request, self.pull_request],
                [passed_check],
                [review],
            )
            agent = FakeAgent()
            monitor = Monitor(
                github=github,
                target="123",
                workdir=Path(directory),
                state_store=state_store,
                agent_launcher=agent,
                runner=github.runner,
            )

            self.assertFalse(monitor.poll_once())
            self.assertEqual(len(agent.prompts), 1)
            self.assertIn("Please handle this edge case.", agent.prompts[0][0])
            self.assertEqual(github.review_calls, 1)

    def test_waiting_ci_also_checks_for_reviews(self):
        review = Review(
            id="review-1",
            author_login="maintainer",
            state="COMMENTED",
            body="Consider a narrower helper.",
            submitted_at="2026-08-14T12:00:00Z",
            commit_sha=self.pull_request.head_sha,
        )
        waiting_check = Check(
            name="ci",
            state="IN_PROGRESS",
            bucket="pending",
            workflow="ci",
            link="",
            started_at="2026-08-14T12:00:00Z",
            completed_at="",
            description="",
        )

        with tempfile.TemporaryDirectory() as directory:
            state_store = StateStore(Path(directory) / "state.json")
            github = FakeGitHub(
                [self.pull_request],
                [waiting_check],
                [review],
            )
            agent = FakeAgent()
            monitor = Monitor(
                github=github,
                target="123",
                workdir=Path(directory),
                state_store=state_store,
                agent_launcher=agent,
                runner=github.runner,
            )

            self.assertFalse(monitor.poll_once())
            self.assertEqual(len(agent.prompts), 1)
            self.assertIn("narrower helper", agent.prompts[0][0])
            self.assertFalse(monitor.poll_once())
            self.assertEqual(len(agent.prompts), 1)

    def test_passed_ci_ignores_own_review_and_stops(self):
        own_review = Review(
            id="review-1",
            author_login="contributor",
            state="COMMENTED",
            body="Self-review note.",
            submitted_at="2026-08-14T12:00:00Z",
            commit_sha=self.pull_request.head_sha,
        )
        passed_check = Check(
            name="ci",
            state="SUCCESS",
            bucket="pass",
            workflow="ci",
            link="",
            started_at="2026-08-14T12:00:00Z",
            completed_at="2026-08-14T12:10:00Z",
            description="",
        )

        with tempfile.TemporaryDirectory() as directory:
            state_store = StateStore(Path(directory) / "state.json")
            github = FakeGitHub(
                [dataclasses.replace(self.pull_request, author_login="contributor")],
                [passed_check],
                [own_review],
            )
            agent = FakeAgent()
            monitor = Monitor(
                github=github,
                target="123",
                workdir=Path(directory),
                state_store=state_store,
                agent_launcher=agent,
                runner=github.runner,
            )

            self.assertTrue(monitor.poll_once())
            self.assertEqual(agent.prompts, [])

    def test_no_checks_and_no_reviews_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            state_store = StateStore(Path(directory) / "state.json")
            github = FakeGitHub([self.pull_request], [], [])
            agent = FakeAgent()
            monitor = Monitor(
                github=github,
                target="123",
                workdir=Path(directory),
                state_store=state_store,
                agent_launcher=agent,
                runner=github.runner,
            )

            self.assertTrue(monitor.poll_once())
            self.assertEqual(agent.prompts, [])


class SynchronizePullRequestTests(unittest.TestCase):
    def test_updates_remote_pr_then_refreshes_local_checkout(self):
        initial = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
        )
        updated = dataclasses.replace(initial, head_sha="def456")
        runner = SynchronizeRunner(
            initial.head_sha,
            refreshed_head_sha=updated.head_sha,
        )
        github = mock.Mock()
        github.get_pull_request.return_value = updated

        result = synchronize_pull_request(
            runner=runner,
            github=github,
            workdir=Path("/tmp/example-repo"),
            pull_request=initial,
        )

        self.assertEqual(result, updated)
        self.assertEqual(
            [command for command, _ in runner.calls],
            [
                ["git", "rev-parse", "HEAD"],
                ["git", "status", "--porcelain"],
                ["gh", "pr", "update-branch", "123"],
                ["gh", "pr", "checkout", "123", "--force"],
                ["git", "rev-parse", "HEAD"],
                ["git", "status", "--porcelain"],
            ],
        )
        github.get_pull_request.assert_called_once_with("123")

    def test_refuses_to_sync_a_dirty_checkout(self):
        pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
        )
        runner = SynchronizeRunner(pull_request.head_sha, dirty=" M file.py")
        github = mock.Mock()

        with self.assertRaises(fix.MonitorError):
            synchronize_pull_request(
                runner=runner,
                github=github,
                workdir=Path("/tmp/example-repo"),
                pull_request=pull_request,
            )

        self.assertEqual(
            [command for command, _ in runner.calls],
            [
                ["git", "rev-parse", "HEAD"],
                ["git", "status", "--porcelain"],
            ],
        )
        github.get_pull_request.assert_not_called()

    def test_stops_when_github_cannot_update_the_base(self):
        pull_request = PullRequest(
            repo="example-org/example-repo",
            number=123,
            title="Example",
            url="",
            state="OPEN",
            merged_at=None,
            head_sha="abc123",
            head_branch="fix-ci",
            base_branch="main",
        )
        runner = SynchronizeRunner(
            pull_request.head_sha,
            update_returncode=1,
            update_stderr="Cannot update PR branch due to conflicts",
        )
        github = mock.Mock()

        with self.assertRaises(fix.CommandError) as context:
            synchronize_pull_request(
                runner=runner,
                github=github,
                workdir=Path("/tmp/example-repo"),
                pull_request=pull_request,
            )

        self.assertIn("Cannot update PR branch due to conflicts", str(context.exception))
        self.assertEqual(
            [command for command, _ in runner.calls],
            [
                ["git", "rev-parse", "HEAD"],
                ["git", "status", "--porcelain"],
                ["gh", "pr", "update-branch", "123"],
            ],
        )
        github.get_pull_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
