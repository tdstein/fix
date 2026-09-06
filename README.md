# fix

`fix` watches a GitHub pull request and launches one interactive agent session
for each batch of new CI failures, reviews, or unresolved inline review
comments from someone other than the pull request author. Before replying to GitHub comments,
it checks the authenticated `gh` user and never replies to comments authored by
that user.

It is intended for a clean local checkout of the pull request branch. The
monitor can update the branch from its configured base branch, launch the
selected agent to repair failures or resolve conflicts, and push validated
changes to the pull request branch.

## Requirements

- Python 3.9 or newer
- [`uv`](https://docs.astral.sh/uv/) for project environments and installation
- Git
- The [GitHub CLI](https://cli.github.com/) (`gh`), authenticated with access
  to the repository
- A supported agent CLI on `PATH`: Codex (`codex`) or Claude Code
  (`claude`). Codex is the default.
- A Unix-like operating system; the monitor uses `fcntl` for process locking

If the pull request changes a GitHub Actions workflow, the `gh` OAuth token
also needs the `workflow` scope. Add it with:

```bash
gh auth refresh --hostname github.com --scopes workflow
```

The selected agent needs permission to commit and push the pull request branch.

## Install

For a project checkout, let `uv` create and synchronize the environment:

```bash
uv sync
```

Run the command from the project without a global install:

```bash
uv run fix
```

To install the command globally in `~/.local/bin`:

```bash
make install
```

This uses `uv tool install`. Make sure `~/.local/bin` is on `PATH`. Remove the
installed command with:

```bash
make uninstall
```

## Usage

Start `fix` from a clean checkout of the repository and pull request branch:

```bash
cd /path/to/repository
git switch my-pr-branch
fix
```

The current directory and branch determine the pull request. The checkout
must already be at the current pull request head, with no uncommitted changes.
You can provide a pull request URL instead; `fix` verifies that the current
directory is the pull request's repository and switches to its head branch
with `gh pr checkout` when needed:

```bash
fix https://github.com/example-org/example-repo/pull/123
```

It exits with an error if the current directory is not a checkout of that
repository.

Choose the agent harness, model, and reasoning effort with flags:

```bash
fix --agent codex --model openai.gpt-5.6-luna --effort max
fix --agent claude --model sonnet --effort max
```

The `--agent` option accepts `codex` or `claude` and defaults to `codex`. The
matching environment variable is `FIX_AGENT`. If no model is selected,
`openai.gpt-5.6-luna` is used for Codex and `sonnet` for Claude Code. The
`FIX_MODEL` value must be valid for the selected agent.

Use `--verbose` to show the full monitor configuration panel instead of the
compact one-line header.

Use `--force-sync` (or `--sync`) to force the pull request branch to be updated
from its configured base branch through GitHub before monitoring starts. The
checkout must still be clean and at the current pull request head.

The matching environment variables are `FIX_MODEL` and `FIX_EFFORT`. Flags take
precedence over environment variables; without either, `fix` uses `max` effort.

`fix` polls every minute. After synchronization advances the pull
request head, it waits for the next poll so GitHub can recognize the new
commit and start its checks. It also performs one immediate follow-up poll
whenever a repair, review, or comment agent exits. It stops when the pull
request closes or when all checks pass without new review feedback. When
checks are waiting or have no failures, it checks pull request reviews and
unresolved inline review threads. Review and comment sessions summarize
feedback with you, apply small clearly correct fixes, and pause for your
judgment on subjective changes. A comment session can resolve a thread after
its concern has been addressed, but it does not reply to self-authored
comments.

In an interactive terminal, `fix` shows a compact monitor summary and
color-coded check and agent statuses. Piped output remains plain and
log-friendly.

Before monitoring an open pull request, `fix` checks its CI status and
mergeability. It updates the branch from the pull request's configured base
branch only when CI has a failure or GitHub reports merge conflicts. If GitHub
reports merge conflicts, it launches a bounded agent session to resolve them
and retries the synchronization. It does not recursively update parent pull
requests in a stack; update those from the root toward the monitored pull
request.

## State and logs

State is stored under `$XDG_STATE_HOME/fix/`,
`$XDG_CACHE_HOME/fix/`, or `~/.cache/fix/`, using one JSON file per pull
request. The state records handled CI failures, reviews, and inline comment
threads. A failed check is suppressed after one repair session for the same
pull request head until CI reports a non-failing state, which prevents a flaky
check from immediately launching duplicate sessions. Agent session logs are
stored in the same directory under `logs/`.

Agent sessions have a fixed two-hour timeout. When an agent reaches that limit,
`fix` terminates the entire agent process group, records the timeout in the
session log, and treats the session as timed out.

CI repair attempts are limited to ten per pull-request head. The count is
stored in persistent state, so restarting `fix` does not reset it. Once the
limit is reached, `fix` suppresses further repair-agent launches for that head
while monitoring continues: it still observes CI, reviews, and unresolved
inline review threads. These limits are fixed operational defaults and do not
have CLI flags or environment-variable overrides.

## Security considerations

Run `fix` only in repositories and worktrees you trust. The repair, review, and
comment agents receive repository contents and diagnostic output, run with the
selected agent's unattended approval and network access settings, and may
commit, push, and resolve review threads.
Review the generated diff and the session logs when investigating unexpected
behavior. Do not run it with credentials or repositories that the agent should
not be able to access.

## Development

The project dependencies are resolved by `uv`. Run the test suite with:

```bash
make test
```

The equivalent direct command is:

```bash
uv run --locked python -m unittest discover -v
```

## License

This project is licensed under the [MIT License](LICENSE).
