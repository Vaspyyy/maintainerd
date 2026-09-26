# Milestone 3 validation

## Exercised locally

78 automated tests passed on Linux with Python 3.13 and Git 2.47. The suite was run in short batches to fit the execution environment's per-command time limit.

Tests use real local Git repositories, separate bare copies, worktrees, SQLite databases, advisory file locks and child processes. A deliberately fake Codex executable implements the tested CLI boundary, and GitHub responses are mocked. No subscription allowance or API billing was used.

Covered cases include:

- A complete finite run, readable report, validated JSON, usage metadata and sourced memory.
- A second run seeing a fresh commit and the first run's observations.
- Dry runs and doctor checks making no model invocation.
- API-key auth and outdated CLI rejection; no automatic credential mutation or unsafe fallback.
- Secret environment variables excluded from Codex, explicit read-only arguments, disabled integrations and optional model selection.
- Malformed results, invalid schemas, failed turns with a zero exit status, quota failures and no retries.
- Timeout termination of a descendant process, interruption records and stale-record recovery.
- Global run-start budgets, pause behavior and locking.
- Worktree integrity failures retained for inspection, unchanged user edits, disabled push URLs and cleanup warnings.
- GET-only model-context GitHub access, unavailable context, response caps and truncation disclosure.
- Proposal issue rendering, one-proposal report limits, idempotent local routing records, crash-marker recovery, deterministic overlap scoring, join-existing behavior, same-maintainer duplicate suppression and closed-history refusal use mocked GitHub App boundaries.
- Discussion contracts require concrete progress for replies, accept other bot comments, ignore self-comments, persist thread events/turns, post validated replies, preserve no_reply behavior and charge discussion turns against the same local run budget.
- Per-maintainer GitHub App configuration overrides the global single-maintainer fallback, and the same external comment can be tracked independently by two maintainers.
- Explicit implementation approval excludes casual design agreement, is limited to the repository owner, and supports clear commands such as `/implement` or `implement it`.
- Claim-first implementation tests cover canonical issue branches, draft-PR creation before writable turns, remote-claim loss without alternate branches, draft-state enforcement, workspace-write invocation, one coherent controller commit, non-force publication boundaries, and the shared launch budget.

Source compilation and a built-wheel/CLI smoke check are also part of the local validation procedure. The GitHub CI workflow runs the offline suite on Python 3.11 and 3.13.

## Not exercised here

- A real logged-in Codex model invocation or actual subscription accounting.
- Linux sandbox enforcement on the user's machine.
- Live private-repository Git credentials or the user's local gh login.
- A live GitHub App installation or real issue publication from the test environment.
- Live GitHub discussion polling/replies from the test environment; those boundaries are mocked.
- A live GitHub branch claim, installation-token Git push, or draft-PR creation from the test environment; GitHub write boundaries are mocked.
- A real workspace-write Codex implementation turn on the user's repository.
- Webhook delivery; foreground polling remains intentional for this milestone.

## Installation-side acceptance check

1. Install, then run `maintainerd doctor`. Resolve missing CLI flags or authentication errors without relaxing sandbox settings.
2. Register the target repository and create a contributor.
3. Run `maintainerd wake mira --dry-run` and review context.json/prompt.txt.
4. Run `maintainerd wake mira`, then `maintainerd show latest`.
5. Confirm the report cites actual SDK files, acknowledges incomplete context, and proposes only useful work.
6. Configure a repository-scoped GitHub App, keep `publish_proposals=false`, run `maintainerd doctor`, then explicitly publish a known-good report with `maintainerd publish RUN_ID`.
7. Only after reviewing that public issue should `publish_proposals=true` be enabled for autonomous proposal creation.
8. Add a normal comment to a maintainer-created issue and run `maintainerd inbox mira`. Confirm a useful comment receives one bot reply and a trivial/self comment does not create a loop.
9. Confirm `maintainerd doctor` reports draft-PR implementation capability after granting Contents and Pull requests read/write to the maintainer App.
10. On a maintainer-owned issue, post an explicit repository-owner command such as `implement it`. Run `maintainerd inbox mira` and verify the canonical branch and **draft PR appear before any implementation commit**.
11. Confirm each later inbox/serve poll adds at most one coherent commit to the same draft PR, never force-pushes, and leaves the PR draft when implementation reports complete.

Passing the controller tests proves mechanics, not good autonomous judgment. Stop autonomous publishing, replies, or implementation if public project quality degrades.
