import dataclasses
import logging
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

# Support both direct and repository-root unittest/pytest invocations.
sys.path.insert(0, str(Path(__file__).parent))

from fix import (
    Check,
    GitHubClient,
    Monitor,
    PrettyFormatter,
    PullRequest,
    StateStore,
    build_agent_command,
    build_agent_prompt,
    build_parser,
    default_state_path,
)


class LoggingTests(unittest.TestCase):
    def test_formatter_is_compact_and_aligned_without_color(self):
        record = logging.LogRecord(
            "fix",
            logging.INFO,
            __file__,
            1,
            "Watching %s",
            ("example-org/example-repo#123",),
            None,
        )

        output = PrettyFormatter().format(record)

        self.assertRegex(
            output,
            r"^\d{2}:\d{2}:\d{2}  INFO  Watching example-org/example-repo#123$",
        )
        self.assertNotIn("\033[", output)

    def test_formatter_indents_multiline_messages(self):
        record = logging.LogRecord(
            "fix",
            logging.ERROR,
            __file__,
            1,
            "command failed\nstderr line",
            (),
            None,
        )

        output = PrettyFormatter().format(record)

        self.assertIn("ERROR command failed\n    stderr line", output)

    def test_formatter_colors_only_the_timestamp_and_level(self):
        record = logging.LogRecord(
            "fix",
            logging.WARNING,
            __file__,
            1,
            "careful",
            (),
            None,
        )

        output = PrettyFormatter(use_color=True).format(record)

        self.assertIn("\033[2m", output)
        self.assertIn("\033[33mWARN \033[0m", output)
        self.assertTrue(output.endswith(" careful"))


class CliTests(unittest.TestCase):
    def test_current_branch_is_the_only_input(self):
        args = build_parser().parse_args([])
        self.assertEqual(vars(args), {})

    def test_pull_request_number_is_not_an_argument(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["123"])


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


class StateStoreTests(unittest.TestCase):
    def test_default_path_uses_fix_namespace(self):
        self.assertEqual(
            default_state_path("example-org/example-repo", 123).parts[-2:],
            ("fix", "example-org-example-repo-pr-123.json"),
        )

    def test_save_and_load_is_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(
                path,
                repo="example-org/example-repo",
                target="123",
                number=123,
            )
            value = store.load()
            value["head_sha"] = "abc"
            store.save(value)
            self.assertEqual(store.load()["head_sha"], "abc")
            self.assertEqual(json.loads(path.read_text())["version"], 1)


class AgentCommandTests(unittest.TestCase):
    def test_command_is_fixed_to_codex_luna_and_max_effort(self):
        workdir = Path("/tmp/example-repo")
        command, mode = build_agent_command(workdir=workdir)
        self.assertEqual(mode, "stdin")
        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--model",
                "openai.gpt-5.6-luna",
                "--config",
                'model_reasoning_effort="max"',
                "--sandbox",
                "workspace-write",
                "--approve-for-me",
                "--strict-config",
                "--config",
                "sandbox_workspace_write.network_access=true",
                "--ephemeral",
                "--cd",
                str(workdir),
                "-",
            ],
        )


class PromptTests(unittest.TestCase):
    def test_prompt_contains_failure_context_and_push_constraints(self):
        pull_request = PullRequest(
            repo="example-org/example-repo",
            target="123",
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
        self.assertIn("failure-key", prompt)
        self.assertIn("fix-ci", prompt)
        self.assertIn("Never force-push", prompt)


class GitHubClientTests(unittest.TestCase):
    def test_initial_pull_request_lookup_uses_current_branch(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def run(self, command, *, cwd=None, input_text=None, timeout=None):
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


class FakeRunner:
    def __init__(self, head_sha):
        self.head_sha = head_sha

    def run(self, command, *, cwd=None, input_text=None, timeout=None):
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, self.head_sha, "")
        if command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


class FakeGitHub:
    def __init__(self, pull_requests, checks):
        self.pull_requests = iter(pull_requests)
        self.last_pull_request = pull_requests[-1]
        self.checks = checks
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
            target="123",
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
            state_store = StateStore(
                Path(directory) / "state.json",
                repo=self.pull_request.repo,
                target=self.pull_request.target,
                number=self.pull_request.number,
            )
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
            self.assertTrue(monitor.poll_once().launched_agent)
            self.assertFalse(monitor.poll_once().launched_agent)
            self.assertEqual(len(agent.prompts), 1)

    def test_new_head_allows_a_new_repair_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            second_head = dataclasses.replace(self.pull_request, head_sha="def456")
            state_store = StateStore(
                Path(directory) / "state.json",
                repo=self.pull_request.repo,
                target=self.pull_request.target,
                number=self.pull_request.number,
            )
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
            self.assertTrue(monitor.poll_once().launched_agent)
            self.assertTrue(monitor.poll_once().launched_agent)
            self.assertEqual(len(agent.prompts), 2)


if __name__ == "__main__":
    unittest.main()
