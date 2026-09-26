# Working on maintainerd

This is a small, Linux-first autonomous-maintainer runtime. Keep it understandable.

- The target is independent, persistent contributors, not a manager/builder/reviewer hierarchy.
- Exploration and discussion Codex turns remain READ-ONLY. Milestone 3 permits workspace-write only after an explicit repository-owner implementation command, a won canonical remote claim, and an already-created draft PR. The trusted controller alone commits/pushes. Do not introduce force-pushes, autonomous merges, workflow edits, repository-setting changes, API billing, or automatic startup.
- Independent rediscovery is allowed. Suppress duplicate publication, not independent thought. Strongly overlapping open work should join the existing thread; closed overlap should require review rather than silently reopen history.
- Bot-to-bot engineering discussion is allowed. Proposal issues are shared across maintainers. Ignore only the current maintainer's own bot comments. A reply must add concrete progress; agreement/repetition should be no_reply. Additional maintainers should use distinct GitHub App identities; a global App remains only a single-maintainer fallback.
- Ready PRs are peer-reviewable once per reviewer/head SHA. Blocking reviews or failed CI return the original author to draft revision under the existing approval. Review feedback never grants a competing writer access and merge remains human-controlled.
- Parallel single-host workers are allowed for distinct maintainers. Keep long-lived locking scoped per maintainer; serialize only short shared bare-Git metadata operations and proposal publication. Never reintroduce a global lock around entire model turns, and never remove the canonical remote implementation claim.
- Codex authentication must remain subscription-backed. Do not read, copy or print auth.json, API keys, token-bearing URLs or private keys. No billing fallback.
- Application memory is sourced and bounded. A model observation is not a human decision. Silence is not approval.
- Do not force a feature/issue/PR quota. A well-reasoned no_action is a valid outcome.
- Future code-writing must follow `docs/IMPLEMENTATION_PROTOCOL.md`: one canonical issue branch is the remote atomic claim, a draft PR must exist before any implementation edit, commits are pushed incrementally, and published history is never force-rewritten.
- Shell commands use argument arrays, never interpolated shell=True strings. Do not echo credential-helper output in exceptions.
- Configuration isolation and read-only sandbox failures are errors, not reasons to relax permissions. Keep clear documentation of residual local-account risks.
- Use the Python standard library unless a new dependency has a concrete, discussed benefit.
- Test Git/SQLite/process behavior with local fixtures. Test AI boundaries with fakes. Never claim mock tests prove real model quality, live GitHub access or sandbox effectiveness.
- Run `PYTHONPATH=src python -m unittest discover -s tests -v` and `python -m compileall -q src tests`.
- Keep README setup commands synchronized with the actual CLI.

- Keep dedupe bookkeeping private. Do not publish ranges of unrelated issue numbers merely to say they were checked; public cross-references must be materially relevant.
- Preserve soft exploration diversity by supplying shared recent fleet coverage instead of assigning permanent maintainer roles.
