# fix

`fix` watches a GitHub pull request and launches one interactive Codex session
for each new CI failure or review from someone other than the pull request
author.

I made this to replace `/goal fix ci`.

Install it to `~/.local/bin`:

```bash
make install
```

Remove the installed command with `make uninstall`.

Run it from a clean checkout of the repository and PR branch:

```bash
cd /path/to/connect
git switch my-pr-branch
fix
```

The current directory and branch determine the pull request. `fix` polls every
five minutes, stops when the pull request closes or all checks pass without a
new review, and stores state under
`$XDG_STATE_HOME/fix/`, `$XDG_CACHE_HOME/fix/`, or `~/.cache/fix/`.

When checks are waiting or have no failures, `fix` also checks the pull request
reviews. A review session summarizes the feedback with you, applies small
clearly correct fixes, and pauses for your judgment on subjective changes.
Review sessions stay interactive until you finish working through the
feedback. Reviews and failed checks are recorded so the same event does not
launch another session.

Before monitoring an open pull request, `fix` asks GitHub to update its branch
from the pull request's configured base branch, then refreshes the local
checkout. This works for stacked pull requests because each PR's declared base
branch is used; only the PR being monitored is updated. The checkout must be
clean and already at the current PR head. If GitHub reports merge conflicts,
`fix` launches a bounded Codex session to resolve them, then retries the
synchronization. It stops if the conflict cannot be resolved safely. It does
not recursively update parent PRs; update the stack from its root toward the
monitored PR when the parent branches are also behind.

The repair agent is always Codex using the Luna model at maximum reasoning
effort. `gh` must be authenticated, `codex` must be on `PATH`, and the repair
agent needs permission to commit and push the PR branch.
