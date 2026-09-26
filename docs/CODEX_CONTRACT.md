# Codex integration contract

Documentation checked on 2026-09-25. Runtime feature detection is used instead of guessing a minimum CLI version. This project has no vendored Codex binary.

Primary references:

- [Non-interactive execution](https://developers.openai.com/codex/noninteractive): saved CLI auth, JSONL events, JSON Schema output, explicit sandbox mode, configuration/rule isolation and final-message files.
- [Authentication](https://developers.openai.com/codex/auth): ChatGPT versus API-key authentication, login status, credential stores, and forced_login_method.
- [CLI reference](https://developers.openai.com/codex/cli/reference): exec flags, strict configuration, ephemeral sessions and command-line overrides.
- [Configuration reference](https://developers.openai.com/codex/config-reference): feature gates, environment inheritance, provider selection and sandbox-related options.
- [GitHub CLI API command](https://cli.github.com/manual/gh_api): explicit GET requests through local GitHub authentication.
- [Git worktree](https://git-scm.com/docs/git-worktree): detached worktrees and controller-managed cleanup.

The current OpenAI documentation redirects some of these URLs to learn.chatgpt.com. The checked interface supports `--ignore-user-config` without replacing CODEX_HOME, so the adapter can ignore personal model/tool settings while keeping saved authentication in place. `--ignore-rules` avoids inheriting permissive execution rules. `--full-auto` is deliberately not used. Exploration and discussion use `--sandbox read-only`; only an explicitly approved implementation turn, after the canonical remote claim and draft PR already exist, may use `--sandbox workspace-write` on its controller-owned task worktree. GitHub credentials are not inherited into either mode.

The adapter parses the final file supplied by `--output-last-message`, not arbitrary streamed assistant text. JSONL is retained for diagnostics and token counters. A failed turn or nonzero exit fails the run even when some model output exists. Unknown JSONL event types are tolerated; the final report has an independently validated, bounded schema.

No real subscription login is bundled or required by the test suite. CLI argument construction, read-only/workspace-write selection, implementation gating and GitHub publication boundaries are covered with fakes/mocks. Real authentication, kernel sandbox behavior, GitHub App Git pushes and model implementation quality still require installation-side smoke tests. Do not represent the offline suite as a live Codex or live write-capable GitHub run.
