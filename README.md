# maintainerd

**A small daemon that turns a subscription-authenticated coding CLI into a persistent software contributor.**

No manager agent, fixed developer roles, API billing integration, Redis, database server, or web dashboard. Just Python, SQLite, Git worktrees, Codex CLI, and one narrowly scoped GitHub App when proposal publishing is enabled.

## What works today

Milestone 3 keeps exploration and discussion **read-only**, but adds an explicitly approved implementation path. The repository owner must use an implementation command in the issue thread; maintainerd then claims the canonical remote branch, creates a draft PR first, and only after that gives Codex a writable task worktree:

```text
manual wake or optional interval
    -> fetch a controller-owned repository copy
    -> create a detached worktree at a recorded commit
    -> collect recent commits and optional GitHub context
    -> load the contributor's sourced notes and recent reports
    -> run Codex with ChatGPT authentication and a read-only sandbox
    -> validate a structured report
    -> save evidence, proposal drafts, limitations and observations
    -> create a new issue OR join a strongly overlapping existing thread
    -> poll owned/joined threads for new human or bot comments
    -> wake the same maintainer for a read-only discussion turn when useful
    -> post at most one validated reply
    -> explicit owner command such as "implement it"
    -> atomically claim maintainerd/issue-N
    -> create DRAFT PR from an empty claim commit
    -> only then grant one workspace-write implementation turn
    -> commit and push one coherent step to the existing draft PR
    -> repeat on later polls until complete or blocked
    -> clean up safe worktrees and stop
```

A run can propose an improvement, identify missing context, or conclude that no action is worthwhile. Producing an issue or PR is not a quota. Speculative features and API changes should become discussions before implementation.

Codex never receives GitHub credentials. Exploration and discussion turns remain read-only. An approved implementation turn uses Codex `workspace-write` only inside a controller-owned task worktree; the trusted controller validates changed paths, creates one commit, and pushes it with a short-lived GitHub App token. **No force-pushes, autonomous merges, workflow edits, or repository-setting changes are allowed.** Polling is still used instead of webhooks.

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
4. Repository permissions for Milestone 3: **Issues: Read and write**, **Contents: Read and write**, and **Pull requests: Read and write**. Metadata read access is automatic. Do not grant Administration, Secrets, Environments, or Workflows write. Actions/Checks read access is optional for later CI-aware behavior.
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

The global App settings above are a convenient fallback for the first maintainer. **Each additional maintainer should get its own GitHub App** so GitHub shows distinct identities and bots can respond to one another instead of treating another maintainer as themselves:

```sh
.venv/bin/maintainerd maintainer create noah
.venv/bin/maintainerd identity set noah \
  --app-id 7654321 \
  --key-path ~/.config/maintainerd/noah.private-key.pem

.venv/bin/maintainerd identity list
.venv/bin/maintainerd doctor
```

A per-maintainer identity overrides the global fallback only for that maintainer. `identity clear noah` returns Noah to the global fallback. `doctor` warns when multiple maintainers resolve to the same bot login because distinct bot-to-bot conversation would not work correctly in that configuration.

The controller signs a short-lived GitHub App JWT with local `openssl`, exchanges it for an installation token, caches that token only in process memory for less than its normal lifetime, and never stores or passes it to Codex.

To publish an existing completed proposal, including a proposal created before Milestone 1:

```sh
.venv/bin/maintainerd publish latest
# or:
.venv/bin/maintainerd publish 38aa1b4f8d1245dc
```

Publication is idempotent for a run. A crash after GitHub accepts the issue is recovered by the embedded run marker. Before opening a new issue, maintainerd compares the proposal against the 100 most recently updated GitHub issues/PRs. Strong overlap with an open thread routes the independent finding into that existing discussion instead of opening a duplicate. Strong overlap with a closed item stops automatic publication so an old decision is not silently reopened. If the same maintainer already owns the overlapping open thread, no duplicate comment is posted.

This deterministic similarity gate is deliberately conservative, not magical semantic search. The exploration prompt also receives open items plus recent closed items and is expected to notice overlap itself. Independent rediscovery is useful; duplicate publication is what gets suppressed.

With `publish_proposals = true`, future successful `propose` runs route automatically after the read-only model process has ended. A publication failure leaves the validated local report intact and records `publication-warning.json`.

## Discussion inbox

Once a proposal is created or joined, maintainerd remembers that thread. Check for new comments manually with:

```sh
.venv/bin/maintainerd inbox mira
```

Human comments and comments from **other bots** are both valid inputs. Only the maintainer's own GitHub App comments are ignored, which prevents self-trigger loops. Multiple new comments on one thread are handled in one Codex turn.

A reply is allowed only when the model's structured result identifies concrete progress such as new evidence, a code location, counterexample, correction, design alternative, synthesis, or concrete decision/question. Pure agreement and repetition should result in `no_reply`. Bot-to-bot discussion is intentionally allowed; a long bot-only streak merely raises the bar for adding another comment.

Discussion turns use a fresh current repository snapshot and the complete fetched issue thread. They consume the same local daily Codex-run budget as exploration turns. No comment is marked processed until the turn completes successfully, so failures do not silently lose maintainer input.

## Explicit implementation approval

Design agreement is deliberately **not** implementation approval. A comment like:

```text
option 1 sounds good
```

resolves the design question but does not authorize source changes.

The repository owner can explicitly hand the issue to the maintainer with phrases such as:

```text
implement it
/implement option 1
go ahead and implement option 1
please implement this
make the PR
```

Only a non-bot comment from the GitHub repository owner can pass this gate in Milestone 3.

After approval, the controller follows [docs/IMPLEMENTATION_PROTOCOL.md](docs/IMPLEMENTATION_PROTOCOL.md):

```text
owner approval
    -> create empty claim commit
    -> atomically create maintainerd/issue-N on GitHub
    -> immediately create DRAFT PR
    -> only now start workspace-write Codex
    -> one coherent change step
    -> controller commits + non-force pushes
    -> next poll continues with another visible commit
```

If another maintainer already won the canonical branch, the loser stops before editing anything. The open draft PR is the implementation lease.

Each implementation turn may make at most one coherent commit. GitHub credentials are never placed in the Codex environment. Changes to `.github/*`, `.git/*`, `AGENTS.md`, `CLAUDE.md`, and `.gitmodules` are blocked. A remote non-fast-forward push is a failure, never a reason to force-push.

Draft PR descriptions are engineering-facing rather than controller-facing. They start with the issue summary/problem/approved direction, keep claim mechanics in a collapsed metadata block, and are refreshed after every implementation turn with progress, reported validation commands, limitations, and current status.

When the implementation reports complete, the PR intentionally remains a **draft** for review rather than being merged or silently marked ready.

Inspect current work with:

```sh
.venv/bin/maintainerd implementations
```

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
    threads/
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

The daily budget counts actual Codex launch attempts, including failed exploration and discussion turns, across all contributors in this data directory. It resets at midnight UTC. Doctor checks, inbox polls with nothing to answer, and dry runs do not consume it. The 15-minute timeout covers each Codex invocation; Git and GitHub preparation have their own shorter timeouts.

## Continuous foreground operation

After manual checks, one simple loop can handle both proactive exploration and reactive discussion:

```sh
.venv/bin/maintainerd serve mira --every-hours 12 --poll-seconds 300
```

Every five minutes it checks Mira's GitHub threads. Polls with nothing to answer cost no Codex usage. If an approved implementation is active, each poll may perform one additional implementation step and push one visible commit; proactive exploration is paused until that implementation is complete or blocked. Otherwise Mira receives a proactive exploration wake every twelve hours even when nothing happened on GitHub.

This runs in the foreground. If no previous exploration exists, the first inspection is immediate. Otherwise the next exploration is based on the last recorded attempt, so restarting does not immediately spend another run. Runtime errors stop the loop instead of retry-burning allowance. Ctrl+C stops the loop and terminates an active Codex process group. Nothing installs itself into systemd yet.

```sh
.venv/bin/maintainerd pause
.venv/bin/maintainerd resume
```

Pause prevents **new** real runs; it does not cancel one already active. Resume does not start a service. Nothing installs an autostart entry, changes account settings, or runs on your second machine. V0.1 is single-host: do not share its SQLite database over a network filesystem.

## GitHub context is deliberately bounded

The host's `gh` login fetches exploration context with GET requests only. The model gets a snapshot, not the GitHub credential or a GitHub write tool. The separate GitHub App credential is used only by trusted host-side publishing and discussion code. The exploration snapshot covers up to 100 open issues/PRs, comments on the five most recently updated open items, 30 recently closed issues/PRs, and ten recent workflow runs.

Limits, unavailable data and truncation are recorded explicitly. PR diffs, inline reviews, older closed history and GitHub Discussions are still incomplete. Missing `gh` or failed authentication produces a code-only inspection with a warning, not a false claim that there are no existing discussions.

## Safety boundary

Read [docs/SAFETY.md](docs/SAFETY.md) before leaving it unattended. The relevant boundary is the installed Codex sandbox, not a VM. Worktrees prevent accidental cross-task edits but are not a security sandbox. Use trusted repositories on a trusted local account for this prototype.

## Tests

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
```

The suite uses actual local Git repositories, SQLite and subprocesses, with a fake Codex executable and mocked GitHub boundary. No AI subscription, API key or network access is needed. It covers successive runs with fresh commits and memory, authentication rejection, safe invocation flags, no-op reports, malformed outputs, interruption, descendant-process termination, quotas, locking, dirty checkout protection, missing GitHub context and truncation disclosure.

See [docs/VALIDATION.md](docs/VALIDATION.md) for what was and was not tested during implementation.

## Coordination rules

The coordination model stays deliberately small:

1. **Ideas are not exclusive.** Independent maintainers may rediscover the same problem or disagree in the same thread.
2. **Publication is deduplicated.** Strongly overlapping discoveries join an existing open issue/PR instead of creating another one.
3. **Discussion is open.** Human-to-bot and bot-to-bot engineering conversation are both valid; only self-comments are ignored.
4. **Implementation is claimed remotely before coding.** All agents competing for issue `#N` attempt the same canonical branch `maintainerd/issue-N`. The first non-force remote creation wins. The winner immediately opens a draft PR from an empty claim commit. **No implementation source may be edited before that draft PR exists.**
5. **Work stays visible while it is happening.** The winner adds coherent commits to the already-open draft PR and pushes them one by one. No force-push or hidden giant final upload. Other maintainers can see partial progress and stop duplicating it.
6. **Parallel implementations are exceptional.** A second implementation branch requires explicit human authorization rather than being an automatic response to disagreement.

The open draft PR is the implementation lease. No manager agent allocates work and no component assigns permanent subsystems to maintainers. The full invariant is documented in [docs/IMPLEMENTATION_PROTOCOL.md](docs/IMPLEMENTATION_PROTOCOL.md).

## Next milestones

1. Exercise the live approval -> claim -> draft PR -> incremental commit loop on a real issue and tune the implementation prompt.
2. Add PR review/revision loops and CI-aware follow-up while keeping human merging as the default.
3. Add stale-claim recovery for claim-only branches whose controller died before creating a PR.
4. Then add a second independent maintainer identity and optionally another machine.

The contributor's purpose remains the same across milestones: notice useful work, investigate it, discuss when appropriate, and continue over time. The surrounding software should stay small enough to understand.
