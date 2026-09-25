# maintainerd

**A small daemon that turns a subscription-authenticated coding CLI into a persistent software contributor.**

No manager agent, fixed developer roles, API billing integration, Redis, database server, or web dashboard. Just Python, SQLite, Git worktrees, Codex CLI, and one narrowly scoped GitHub App when proposal publishing is enabled.

## What works today

Milestone 1 keeps Codex **read-only** while optionally giving the trusted host controller one public action: create a proposal issue:

```text
manual wake or optional interval
    -> fetch a controller-owned repository copy
    -> create a detached worktree at a recorded commit
    -> collect recent commits and optional GitHub context
    -> load the contributor's sourced notes and recent reports
    -> run Codex with ChatGPT authentication and a read-only sandbox
    -> validate a structured report
    -> save evidence, proposal drafts, limitations and observations
    -> optionally publish one validated proposal as a GitHub issue
    -> clean up the unchanged worktree and stop
```

A run can propose an improvement, identify missing context, or conclude that no action is worthwhile. Producing an issue or PR is not a quota. Speculative features and API changes should become discussions before implementation.

Codex itself still cannot write GitHub or repository files. Proposal publication happens only in trusted controller code using a scoped GitHub App. **Milestone 1 does not reply to comments, modify code, push branches, open PRs, merge anything, or process webhooks.** Those capabilities come later.

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

## Give a maintainer a GitHub issue identity

Keep proposal publishing disabled until you are happy with local reports. Then create a GitHub App for the maintainer:

1. GitHub **Settings -> Developer settings -> GitHub Apps -> New GitHub App**.
2. Give it a distinct name such as `mira-maintains`. A homepage URL can point at this repository.
3. Webhooks are not used yet, so disable **Active** under Webhook.
4. Repository permissions: **Issues: Read and write**. Metadata read access is automatic. Do not grant Contents, Actions, Administration, Secrets or Pull requests for Milestone 1.
5. Install the App only on the managed repository, for example `Vaspyyy/hoi4-agent-sdk`.
6. Generate a private key, move it somewhere outside all repositories, and restrict it:

```sh
mkdir -p ~/.config/maintainerd
mv ~/Downloads/*.private-key.pem ~/.config/maintainerd/mira.private-key.pem
chmod 600 ~/.config/maintainerd/mira.private-key.pem
```

Copy the numeric **App ID** from the GitHub App settings page. No installation ID is needed; maintainerd resolves the installation for the registered repository.

Edit `~/.local/share/maintainerd/config.toml`:

```toml
publish_proposals = true
github_app_id = 123456
github_private_key_path = "/home/ransom/.config/maintainerd/mira.private-key.pem"
```

Then verify the exact installation and permission before publishing:

```sh
.venv/bin/maintainerd doctor
```

The controller signs a short-lived GitHub App JWT with local `openssl`, exchanges it for an installation token, uses that token only in the host process, and never stores or passes it to Codex.

To publish an existing completed proposal, including a proposal created before Milestone 1:

```sh
.venv/bin/maintainerd publish latest
# or:
.venv/bin/maintainerd publish 38aa1b4f8d1245dc
```

Publication is idempotent for a run. A crash after GitHub accepts the issue is recovered by the embedded run marker, and an unrelated open issue with the same title blocks automatic duplication. With `publish_proposals = true`, future successful `propose` runs publish automatically after the read-only model process has ended. A publication failure leaves the validated local report intact and records `publication-warning.json`.

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
publish_proposals = false
# github_app_id = 123456
# github_private_key_path = "/home/ransom/.config/maintainerd/mira.private-key.pem"
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

The host's `gh` login fetches model context with GET requests only. The model gets a snapshot, not the GitHub credential or a GitHub write tool. The separate GitHub App credential is used only by the trusted proposal publisher. The snapshot covers up to 100 open issues/PRs, up to 20 conversation comments on each of the five most recently updated items, and ten recent workflow runs.

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

1. Run proposal-only autonomy for a while and evaluate whether public issue quality stays high.
2. Add GitHub event handling and narrow, thread-owned replies so a human can discuss proposals with the same maintainer identity. Silence is not approval.
3. Add explicit approval state, then approved-work implementation, tests, branch publication, draft PRs and review follow-up. Human merging remains the default.
4. Only then add another independent contributor or a second machine.

The contributor's purpose remains the same across milestones: notice useful work, investigate it, discuss when appropriate, and continue over time. The surrounding software should stay small enough to understand.
