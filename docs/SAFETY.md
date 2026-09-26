# Safety and operational boundaries

## What is enforced by this application

The Codex process never receives GitHub credentials or a direct GitHub write tool. Exploration/discussion turns are read-only. Milestone 3 adds a narrowly gated workspace-write implementation turn only after the repository owner explicitly requests implementation, the canonical remote issue branch has been atomically claimed, and a draft PR already exists. The trusted controller validates paths, commits, performs non-force pushes, and marks a completed draft PR ready for review with a short-lived GitHub App token that never enters Codex. **Ready for review is not merge authorization.** Registered user source checkouts remain untouched.

Codex receives a read-only sandbox request, approval_policy=never, disabled hosted web search, and disabled apps, plugins, hooks, subagents, goals and built-in memory features. User config and execution-policy rules are ignored. It starts in a fresh controller directory outside the target tree, so target-local .codex configuration is not selected as the starting configuration. Missing required CLI flags fail preflight. Unknown settings fail strict configuration parsing instead of being silently accepted.

The Codex environment is allowlisted. API keys, provider overrides, GitHub tokens and SSH agent sockets are not inherited. Cached ChatGPT authentication is reused, and the actual invocation explicitly requires ChatGPT authentication. No API fallback exists.

A single-host advisory lock serializes mutation/run operations. The active Codex process inherits the lock descriptor, which helps prevent a replacement controller from launching over an orphaned worker after an abrupt parent exit. Normal Ctrl+C, SIGTERM and timeout paths terminate the Codex process group. A subsequent operation marks incomplete historical records interrupted instead of pretending they succeeded.

The controller validates the final report's complete shape and checks the worktree HEAD and dirty state before accepting observations into memory. Invalid reports, failed turns and dirty-worktree runs are not successful contributions. Timeouts and failures are not automatically retried.

## What is NOT guaranteed

This is not a container or a separate operating-system account. Codex still runs as your user. Its own authentication necessarily remains accessible to the Codex process. Removing credentials from an environment is not the same as hiding credential files on disk.

The read-only command sandbox is implemented by your installed Codex version and OS. A read-only filesystem policy does not by itself prevent reading unrelated local files. This application has not implemented a credential-excluding filesystem namespace. Use a dedicated account or hardened container before expanding to untrusted repositories or write-capable unattended work.

System/organization-managed Codex settings and future CLI changes may affect behavior. The current compatibility check verifies flags and stored auth mode, not every managed configuration or the kernel sandbox. Strict configuration or sandbox errors must remain failures. Do not work around them with --yolo, danger-full-access or workspace-write.

Disabling a remote push URL is defense in depth, not Git authorization: a process with unrestricted network access could use another URL. Worktrees also share their controller-owned object database. They isolate normal work, not malicious code. The read-only sandbox and disabled integrations are essential to the prototype's intended limits.

Repository text, AGENTS files, comments and previous model notes may contain prompt injection. The prompt treats them as evidence, not authority, but prompt wording is not a security boundary. Model-generated reports can also be wrong, contain unsafe suggestions, or expose sensitive code. Automatic proposal publishing and discussion replies therefore have a real public-output risk: enable them only after reviewing local behavior, keep the GitHub App scoped to the intended repository, and disable `publish_proposals` or stop the foreground loop if judgment quality degrades. Bot comments are not trusted merely because they are bots; they remain untrusted discussion input.

The daily launch budget is per local data directory. It cannot measure or limit all activity on your ChatGPT account. It does not control subscription overage/credit settings. A running model can consume significant allowance before a timeout. Do not interpret token counters as accurate subscription-limit accounting.

## Logs and cleanup

Run artifacts may contain private source excerpts and discussion text. They live outside the public source repository with restrictive creation permissions. Do not publish the entire data directory or full runtime logs without reviewing/redacting them. Existing custom data-directory permissions remain your responsibility.

Normal clean worktrees are removed. Dirty worktrees, hard-kill residue and cleanup failures may require inspection and manual cleanup. No recursive cleanup is performed on the user's source checkout. Retention is manual in this milestone; long-running installations will accumulate logs and reports.

Pause stops new starts, not an active run. Use Ctrl+C for a foreground run. Do not run multiple daemon instances against separate homes expecting one shared quota or coordination system.

## Current write boundary and later milestones

Proposal routing is idempotent per run and uses a conservative similarity gate over recent GitHub issues/PRs. Thread comments are keyed per maintainer so multiple independent maintainers can observe the same human or bot comment later. A maintainer ignores only comments authored by its own GitHub App identity. Give independent maintainers distinct Apps if they are expected to converse; sharing one App makes them intentionally indistinguishable on GitHub. Private keys should be mode 0600 and stored outside repositories. Each App still needs only Metadata read and Issues read/write.

Before enabling code writes, add explicit human-approval records, stronger filesystem/OS isolation for write-capable execution, protected target branches, and the claim-first implementation protocol in `docs/IMPLEMENTATION_PROTOCOL.md`. The remote canonical branch creation is the cross-machine implementation lock; the winner must create a draft PR before source edits and then push coherent commits incrementally without force-rewriting history. Workflows, secrets, permissions and bot policy changes must remain human-controlled. Publishing code or merging must never be inferred from silence, an encouraging comment, or another agent's approval.
