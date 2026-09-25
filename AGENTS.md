# Working on maintainerd

This is a small, Linux-first autonomous-maintainer runtime. Keep it understandable.

- The target is independent, persistent contributors, not a manager/builder/reviewer hierarchy.
- V0.1 is READ-ONLY in managed repositories. Do not quietly introduce GitHub writes, workspace-write execution, API billing, auto-merging, or automatic startup.
- Codex authentication must remain subscription-backed. Do not read, copy or print auth.json, API keys, token-bearing URLs or private keys. No billing fallback.
- Application memory is sourced and bounded. A model observation is not a human decision. Silence is not approval.
- Do not force a feature/issue/PR quota. A well-reasoned no_action is a valid outcome.
- Shell commands use argument arrays, never interpolated shell=True strings. Do not echo credential-helper output in exceptions.
- Configuration isolation and read-only sandbox failures are errors, not reasons to relax permissions. Keep clear documentation of residual local-account risks.
- Use the Python standard library unless a new dependency has a concrete, discussed benefit.
- Test Git/SQLite/process behavior with local fixtures. Test AI boundaries with fakes. Never claim mock tests prove real model quality, live GitHub access or sandbox effectiveness.
- Run `PYTHONPATH=src python -m unittest discover -s tests -v` and `python -m compileall -q src tests`.
- Keep README setup commands synchronized with the actual CLI.
