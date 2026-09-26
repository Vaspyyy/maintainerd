# Hydra v1

Hydra is the opt-in tiered execution mode introduced in maintainerd 0.8.0. Private scouts inspect code, a maintainer core independently validates their findings, and only core-validated proposals reach the existing GitHub publication workflow. Difficult candidate validation can use one explicitly configured escalation turn.

The existing `serve`, `inbox`, `wake`, identities, proposal routes and implementation PRs remain available. **Ordinary `maintainerd serve` is still the legacy mode. Use `maintainerd hydra serve` for Hydra routing, private scouts, concurrency admission and resilient supervision.** Stop legacy workers before starting the same maintainers in Hydra.

## Start with the existing repository and identities

After installing this branch/release, with Codex already logged in through ChatGPT:

```sh
.venv/bin/maintainerd --version
.venv/bin/maintainerd doctor
.venv/bin/maintainerd hydra init
.venv/bin/maintainerd hydra profiles
```

`hydra init` creates `hydra.toml` inside the existing data home, normally `~/.local/share/maintainerd/`. It refuses to overwrite an existing file. It does not rewrite `config.toml`, move App keys, recreate maintainers, reset run history, or replace existing implementation branches.

The default requested profiles are:

| Stage | Requested model | Reasoning effort |
| --- | --- | --- |
| scout | gpt-6-luna | medium |
| explore | gpt-6-luna | medium |
| discuss | gpt-6-luna | medium |
| validate | gpt-6-sol | medium |
| implement | gpt-6-sol | medium |
| review | gpt-6-sol | medium |
| escalate | gpt-6-astra | medium |

These are configurable IDs, not a claim that this development environment verified your account's live model access. The adapter supplies `--model` and `model_reasoning_effort` explicitly. Your model selection in an unrelated interactive Codex session does not override Hydra profiles. Unsupported models or efforts fail visibly; there is no silent expensive-model, API-billing or credential fallback.

To generate different initial profiles, pass `--scout-model`, `--core-model`, and `--escalation-model` to `hydra init`. Set `--escalation-model ""` to omit that profile, or set `max_escalations_per_day = 0` afterward. Edit individual stage tables for finer routing.

For a first live smoke test, before starting the fleet:

```sh
.venv/bin/maintainerd hydra scout --repo hoi4-agent-sdk
.venv/bin/maintainerd hydra pool
.venv/bin/maintainerd hydra validate mira
```

The scout may correctly produce no candidate. Validation may correctly reject a candidate. Neither command guarantees useful findings. Validation can publish only when the existing `config.toml` has `publish_proposals = true`; otherwise an accepted candidate stays private. This does not change the separate requirement for human implementation approval.

Start the fleet:

```sh
.venv/bin/maintainerd hydra serve mira noah iris --scouts 6
```

No new GitHub Apps are needed for scouts. Mira, Noah and Iris keep their existing identities and approved PR work.

## Worker count is not concurrency

The generated configuration contains:

```toml
[hydra]
scouts = 6
max_parallel = 6
reserved_core_slots = 3
scout_interval_seconds = 120
poll_seconds = 30
queue_limit = 30
max_validation_attempts = 3
max_escalations_per_day = 2
core_explore = false
core_explore_seconds = 3600
rate_limit_cooldown_seconds = 120
max_transient_retries = 2
```

Six scout workers plus three core workers means nine controller processes, **not nine simultaneous model calls**. At most six Hydra Codex invocations can be admitted, with three slots reserved for core work. Scouts use only the non-reserved slots. This prevents cheap scanning from occupying all capacity while a maintainer needs to fix a PR.

`--scouts` overrides the worker count for that supervisor. `max_parallel` in `hydra.toml` controls simultaneous Hydra model calls. Increase them separately and restart after configuration changes. The parser permits larger values, but 100-worker operation and provider-side concurrency are not certified by the tests.

`max_runs_per_day` remains in the existing `config.toml`: zero removes the local daily launch cap. That does not remove provider limits. Pre-Hydra launch history is included in local accounting without counting a promoted validation report as a second model invocation.

Core exploration is off by default. Existing issue discussions, approved implementation/revision work and peer review take priority. When those produce no activity, the core validates private candidates. This avoids keeping the stronger tier busy with an expensive exploration loop while the scouts are doing the searching.

## Private finding lifecycle

```text
scout inspection of a recorded commit
    -> private finding with paths, symbols, root cause and suggested reproduction
    -> conservative clustering and retained scout provenance
    -> one core claims validation
    -> fresh repository snapshot and GitHub context
       -> reject / duplicate: private decision, no public issue
       -> needs_input: private decision for operator inspection
       -> escalate: at most one configured stronger validation turn
       -> confirm: core-generated proposal report
    -> existing idempotent issue publisher, when publication is enabled
    -> human issue approval
    -> existing canonical branch + draft-first implementation/review workflow
```

Scouts and validation turns are read-only. A suggested reproduction is **not an executed test**. The core verifies source and existing tests but does not run project code during validation. Public report limitations record this distinction.

Clustering requires compatible root-cause evidence and overlapping paths, not merely that two reports mention `mod.py`. It is intentionally conservative and can miss semantic duplicates. The core's GitHub snapshot and existing publication-time duplicate gate remain additional checks.

The queue counts outstanding clusters, not every scout observation. When full, scouts stop launching new model turns. Concurrent submissions enforce the bound transactionally. Excess results from a turn already in flight remain in that run's local artifacts. Each cluster retains bounded corroborating observations.

Claims combine an OS file lock and a database ownership token. A live validator cannot lose ownership merely because a wall-clock timeout elapsed. Its lock descriptor is inherited by Codex. An interrupted claim becomes retryable; stale tokens cannot promote results. Attempts are bounded, and repeated uncertainty eventually becomes `needs_input` rather than an infinite escalation loop.

Before an accepted private finding is published, changed source sends it back for core validation. The stable proposal report ID survives retries, allowing the existing broker to recover an issue accepted remotely before local publication bookkeeping completed.

## Quota and failures

Hydra distinguishes a worker fault from a shared provider-usage failure.

An unknown runtime, authentication, sandbox or configuration error halts the affected worker visibly. Other workers continue. It does not relax the sandbox or retry indefinitely.

A recognized transient rate limit permits a bounded cooldown/retry using the configured model and same account. A recognized exhausted-usage error opens a shared provider circuit: **no new Hydra model launches**, while already-running turns may finish. It does not repeatedly send 100 calls into the same exhausted subscription or switch models/accounts to evade the limit.

After the actual provider allowance has reset:

```sh
.venv/bin/maintainerd hydra resume
```

This clears only Hydra's local provider-pause flag. It does not reset the subscription. Workers still running in paused state resume polling; failed workers are not silently restarted. Fix their recorded cause and restart the supervisor to bring them back.

`maintainerd pause` and `maintainerd resume` still control the separate local PAUSED flag. Ctrl+C on Hydra terminates its worker controllers, whose normal signal handlers terminate active Codex process groups. Abrupt OS kills are not equivalent to a clean stop; inherited locks protect against duplicate active work, and logs/worktrees should be inspected after such failures.

## Status and inspection

```sh
.venv/bin/maintainerd hydra profiles
.venv/bin/maintainerd hydra status
.venv/bin/maintainerd hydra status --json
.venv/bin/maintainerd hydra pool
.venv/bin/maintainerd hydra pool CANDIDATE_ID
.venv/bin/maintainerd implementations
```

Logs identify the actor, stage, requested model, effort and run ID. Status distinguishes running, idle, waiting for capacity, queue backpressure, provider pause and failure. Pool inspection shows private provenance and decisions. No scout issue number is invented because scouts do not publish.

Token totals are observations from CLI events, not a subscription percentage or dollar estimate. Cached input is a subset of input and is not added a second time. API/enterprise rate-card ratios do not establish how many included subscription runs this setup buys. Measure actual account usage and accepted findings before scaling.

## Boundaries and validation

Hydra v1 is one repository per supervisor on one Linux host with local SQLite. It is not Hydra Atlas, a distributed scheduler, a systemd installer, an automatic merger, or a proven 100-agent deployment.

Scouts have no configured App identity and no publication code path. Model subprocess environments do not receive API/GitHub keys. This is still the existing trusted-local-account security model: read-only Codex and separate worktrees are not a VM or a guarantee that unrelated readable home files are inaccessible. Use trusted repositories and accounts; stronger OS isolation remains separate work.

The offline suite exercises real Git copies/worktrees, SQLite, multiple processes, ownership locks and fake Codex executions. Tests cover model routing, rejection/private-only behavior, concurrent clustering, bounded queues, atomic local budgets, quota pauses, stale validation, one-time escalation, failure-isolated supervision and legacy compatibility. GitHub publication is mocked. CI verifies the combined codebase on Python 3.11 and 3.13.

A live subscription model call, sandbox enforcement on your machine, provider concurrency, real savings, and autonomous judgment quality still require the installation-side smoke test. The implementation session did not launch your fleet or modify the SDK's existing PRs.
