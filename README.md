# maintainerd

**A small daemon that turns a subscription-authenticated coding CLI into a persistent software contributor.**

No manager agent, fixed developer roles, API billing integration, Redis, database server, or web dashboard. Just Python, SQLite, Git worktrees and Codex CLI. GitHub becomes the shared public workspace in later milestones.

## What works today

Milestone 0 is a runnable **read-only exploration loop**:

```text
manual wake or optional interval
    -> fetch a controller-owned repository copy
    -> create a detached worktree at a recorded commit
    -> collect recent commits and optional GitHub context
    -> load the contributor's sourced notes and recent reports
    -> run Codex with ChatGPT authentication and a read-only sandbox
    -> validate a structured report
    -> save evidence, proposal drafts, limitations and observations
    -> clean up the unchanged worktree and stop
```

A run can propose an improvement, identify missing context, or conclude that no action is worthwhile. Producing an issue or PR is not a quota. Speculative features and API changes should become discussions before implementation.

**This version does not open issues, post comments, write code, push branches, or open/merge PRs in managed repositories.** Proposal drafts are local reports. It does not yet process GitHub webhooks. Those capabilities come after the first contributor's judgment has been evaluated.

## Install

Requirements: Linux, Python 3.11+, Git, and a current Codex CLI already signed into ChatGPT. `gh` is optional but recommended for the private SDK's issue/PR/CI context.

```sh
git clone https://github.com/Vaspyyy/maintainerd.git
cd maintainerd
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/maintainerd doctor
```

There are **zero third-party Python runtime dependencies**. Installation uses setuptools. Calling the virtual environment executables directly works without shell-specific activation commands.

`doctor` checks Git, Codex's required automation flags, stored ChatGPT authentication, and optional GitHub CLI authentication. It does not call a model or spend inference allowance. It cannot prove the installed Codex sandbox works; the first real wake is the live integration check.

A successful ordinary `codex` session alone is not enough: old CLI versions may lack the configuration-isolation flags used here. The doctor reports missing flags rather than falling back to an unsafe invocation.

## First inspection of hoi4-agent-sdk

```sh
.venv/bin/maintainerd repo add https://github.com/Vaspyyy/hoi4-agent-sdk.git
.venv/bin/maintainerd maintainer create mira

# Prepare the actual repository snapshot and prompt, but do not call Codex.
.venv/bin/maintainerd wake mira --dry-run

# The first real subscription-backed inspection.
.venv/bin/maintainerd wake mira
.venv/bin/maintainerd show latest
```

Private repository access uses your normal local Git credentials. The GitHub connection in ChatGPT does **not** authenticate your terminal. For an HTTPS setup using GitHub CLI, authenticate locally with `gh auth login`, then run `gh auth setup-git`. No credentials should be pasted into this repository, a command-line URL, or chat.

An existing local checkout is also supported:

```sh
.venv/bin/maintainerd repo add /path/to/hoi4-agent-sdk \
  --name hoi4-agent-sdk --github Vaspyyy/hoi4-agent-sdk
```

This copies committed branch state into a separate bare repository, without hard links. It does not change your checkout, index, uncommitted edits, or untracked files. Subsequent fetches follow the registered **local source**, not its upstream; update that source yourself when necessary. Use the remote URL to follow GitHub directly. Submodules and Git LFS payloads are not automatically downloaded.

When only one repository exists, `maintainer create mira` selects it. Otherwise use `--repo NAME`. No schedule starts automatically.

## See what happened

```sh
.venv/bin/maintainerd runs
.venv/bin/maintainerd show latest
.venv/bin/maintainerd show latest --json
.venv/bin/maintainerd memory list mira
.venv/bin/maintainerd memory add mira "Prefer additive APIs; discuss semantic changes first."
.venv/bin/maintainerd memory forget mira 3
```

Every run records its base commit, timestamps, status, prompt, context snapshot, JSONL events, stderr, validated JSON result, and readable Markdown report when successful. `show --json` includes usage fields emitted by Codex. Token counts are **not** a percentage of remaining subscription allowance.

Human notes and agent observations have separate provenance. Only successful, validated reports add agent memory. Recent context is bounded to five successful reports and twenty notes from each source category. Historical artifacts remain on disk. This is continuity, not an immortal chat session or a vector database.

By default clean worktrees are removed after a run, while reports and the bare repository remain. Use `wake mira --keep-worktree` to inspect the exact checkout afterward. Dirty worktrees or cleanup failures are retained and recorded; they are not force-deleted.

## Subscription-only behavior

The controller requires a positive ChatGPT login status. It removes API keys and other provider credentials from the Codex process environment, enforces `forced_login_method="chatgpt"`, and uses the OpenAI provider. It does not implement an API client or paid API fallback.

Saved authentication is reused in place. Credential files are not copied, printed, or committed. Your normal Codex configuration is not rewritten. A preflight that sees API-key authentication refuses the run without deliberately logging you out.

**Subscription usage is still finite.** The daily run-start budget is local to this maintainerd installation, not a measurement of your plan allowance. It cannot stop unrelated Codex sessions, or enforce whether your ChatGPT account has purchased credits or account-level paid usage enabled. Check those account settings separately. Quota/auth/sandbox failures stop the run without an automatic retry or a billing-mode switch.

## Configuration

The first command creates:

```text
~/.local/share/maintainerd/
    config.toml
    state.sqlite3
    repos/
    workspaces/
    runs/
```

`XDG_DATA_HOME` is respected. Override the directory with `MAINTAINERD_HOME` or the global `--home PATH` option, placed before the subcommand. State and logs are private application data, not repository files. The CLI uses a restrictive umask.

```toml
codex_binary = "codex"
timeout_seconds = 900
max_runs_per_day = 4
include_github = true
# model = "a-model-id-available-in-your-Codex-subscription"
```

The model is deliberately not hardcoded. With `model` omitted, Codex chooses its default; it does **not** inherit a model selection from your ignored personal `config.toml`. Use this application configuration to make the choice explicit. Unknown configuration keys are rejected, including API-key settings.

The daily budget counts actual Codex launch attempts, including failed launches, across all contributors in this data directory. It resets at midnight UTC. Doctor checks and dry runs do not consume it. The 15-minute timeout covers the Codex invocation; Git and GitHub preparation have their own shorter timeouts.

## Optional automatic exploration

After reviewing a few manual runs, the same finite cycle can run on a timer even when no GitHub activity occurs:

```sh
.venv/bin/maintainerd serve mira --every-hours 12
```

This runs in the foreground. If no previous real run exists, the first inspection is immediate. Otherwise its next due time is based on the last recorded attempt, so restarting the process does not immediately spend another run. The loop stops on runtime errors rather than repeatedly consuming allowance. Ctrl+C stops the loop and terminates an active Codex process group.

```sh
.venv/bin/maintainerd pause
.venv/bin/maintainerd resume
```

Pause prevents **new** real runs; it does not cancel one already active. Resume does not start a service. Nothing installs an autostart entry, changes account settings, or runs on your second machine. V0.1 is single-host: do not share its SQLite database over a network filesystem.

## GitHub context is deliberately bounded

The host's `gh` login fetches data with GET requests only. The model gets a snapshot, not the GitHub credential or a GitHub write tool. The snapshot covers up to 100 open issues/PRs, up to 20 conversation comments on each of the five most recently updated items, and ten recent workflow runs.

Limits, unavailable data and truncation are recorded explicitly. Closed proposals, PR diffs, inline reviews and GitHub Discussions are not fetched yet. Missing `gh` or failed authentication produces a code-only inspection with a warning, not a false claim that there are no open issues. This is **not yet exhaustive duplicate detection**.

## Safety boundary

Read [docs/SAFETY.md](docs/SAFETY.md) before leaving it unattended. The relevant boundary is the installed Codex sandbox, not a VM. Worktrees prevent accidental cross-task edits but are not a security sandbox. Use trusted repositories on a trusted local account for this prototype.

## Tests

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
```

The suite uses actual local Git repositories, SQLite and subprocesses, with a fake Codex executable and mocked GitHub boundary. No AI subscription, API key or network access is needed. It covers successive runs with fresh commits and memory, authentication rejection, safe invocation flags, no-op reports, malformed outputs, interruption, descendant-process termination, quotas, locking, dirty checkout protection, missing GitHub context and truncation disclosure.

See [docs/VALIDATION.md](docs/VALIDATION.md) for what was and was not tested during implementation.

## Next milestones

1. Evaluate real read-only reports and refine initiative, evidence and memory.
2. Add a GitHub App identity and a narrow broker for proposal issues and replies, with explicit approval state. Silence is not approval.
3. Add approved-work implementation, tests, branch publication, draft PRs and review follow-up. Human merging remains the default.
4. Only then add another independent contributor or a second machine.

The contributor's purpose remains the same across milestones: notice useful work, investigate it, discuss when appropriate, and continue over time. The surrounding software should stay small enough to understand.
