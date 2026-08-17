# fix

`fix` watches a GitHub pull request and launches one interactive Codex session
for each new CI failure or review from someone other than the pull request
author.

It is intended for a clean local checkout of the pull request branch. The
monitor can update the branch from its configured base branch, launch Codex to
repair failures or resolve conflicts, and push validated changes to the pull
request branch.

## Requirements

- Python 3.9 or newer
- [`uv`](https://docs.astral.sh/uv/) for project environments and installation
- Git
- The [GitHub CLI](https://cli.github.com/) (`gh`), authenticated with access
  to the repository
- The Codex CLI (`codex`) on `PATH`, with access to the configured
  `openai.gpt-5.6-luna` model
- A Unix-like operating system; the monitor uses `fcntl` for process locking

If the pull request changes a GitHub Actions workflow, the `gh` OAuth token
also needs the `workflow` scope. Add it with:

```bash
gh auth refresh --hostname github.com --scopes workflow
```

The Codex agent needs permission to commit and push the pull request branch.

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

Choose the Codex model and reasoning effort with flags:

```bash
fix --model openai.gpt-5.6-luna --effort max
```

The matching environment variables are `FIX_MODEL` and `FIX_EFFORT`. Flags take
precedence over environment variables; without either, `fix` uses
`openai.gpt-5.6-luna` and `max`.

`fix` polls every five minutes. After synchronization advances the pull
request head, it waits for the next poll so GitHub can recognize the new
commit and start its checks. It also performs one immediate follow-up poll
whenever a repair or review agent exits. It stops when the pull request closes
or when all checks pass without a new review. When checks are waiting or have
no failures, it also checks pull request reviews. A review session summarizes
feedback with you, applies small clearly correct fixes, and pauses for your
judgment on subjective changes.

In an interactive terminal, `fix` shows a compact monitor summary and
color-coded check and agent statuses. Piped output remains plain and
log-friendly.

Before monitoring an open pull request, `fix` checks its CI status and
mergeability. It updates the branch from the pull request's configured base
branch only when CI has a failure or GitHub reports merge conflicts. If GitHub
reports merge conflicts, it launches a bounded Codex session to resolve them
and retries the synchronization. It does not recursively update parent pull
requests in a stack; update those from the root toward the monitored pull
request.

## State and logs

State is stored under `$XDG_STATE_HOME/fix/`,
`$XDG_CACHE_HOME/fix/`, or `~/.cache/fix/`, using one JSON file per pull
request. Agent session logs are stored in the same directory under `logs/`.

## Security considerations

Run `fix` only in repositories and worktrees you trust. The repair and review
agents receive repository contents and diagnostic output, run with Codex
approval enabled and network access enabled, and may commit and push changes.
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
uv run python -m unittest discover -v
```

## License

This project is licensed under the [MIT License](LICENSE).
