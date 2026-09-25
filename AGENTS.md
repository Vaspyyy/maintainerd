# Working on maintainerd

This is a small, Linux-first autonomous-maintainer runtime. Keep it understandable.

- The target is independent, persistent contributors, not a manager/builder/reviewer hierarchy.
- Codex remains READ-ONLY in managed repositories. Milestone 2 permits only host-brokered proposal issue creation and top-level issue/PR conversation comments through a scoped GitHub App. Do not quietly introduce code writes, pushes, PR creation, API billing, auto-merging, repository-setting changes, or automatic startup.
- Independent rediscovery is allowed. Suppress duplicate publication, not independent thought. Strongly overlapping open work should join the existing thread; closed overlap should require review rather than silently reopen history.
- Bot-to-bot engineering discussion is allowed. Ignore only the current maintainer's own bot comments. A reply must add concrete progress; agreement/repetition should be no_reply. Additional maintainers should use distinct GitHub App identities; a global App remains only a single-maintainer fallback.
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
