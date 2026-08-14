# fix

`fix` watches a GitHub pull request and launches one non-interactive Codex
repair session for each new CI failure.

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
five minutes, stops when the pull request closes or all checks pass, and stores
state under
`$XDG_STATE_HOME/fix/`, `$XDG_CACHE_HOME/fix/`, or `~/.cache/fix/`.

The repair agent is always Codex using the Luna model at maximum reasoning
effort. `gh` must be authenticated, `codex` must be on `PATH`, and the repair
agent needs permission to commit and push the PR branch.
