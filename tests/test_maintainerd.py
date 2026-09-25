"""Offline tests: real Git/SQLite/processes, fake Codex and GitHub boundaries."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from maintainerd import cli, codex, cycle, github, repo, report
from maintainerd.state import Busy, Config, Error, State, name, utcnow, write_json


RESULT = {
    "outcome": "propose",
    "summary": "A deliberately fake result for exercising the controller, not an actual repository finding.",
    "findings": [{"title": "Discuss input validation", "problem": "An example input needs investigation.",
                  "evidence": ["README.md:1 (test fixture)"], "proposal": "Discuss expected behavior first.",
                  "tradeoffs": "Avoid changing public semantics without a decision.", "questions": ["Is this desirable?"]}],
    "inspected_paths": ["README.md"], "limitations": ["Fake Codex. Tests were not run by the agent."],
    "memory_notes": ["Test observation: inspect README.md against the current commit before reusing this note."],
}


FAKE_CODEX = r'''
import json, os, subprocess, sys, time
from pathlib import Path
behavior = json.loads(Path(BEHAVIOR).read_text())
args = sys.argv[1:]
with Path(TRACE).open("a") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("codex-fake 0.0-test")
    sys.exit(0)
if "--help" in args:
    print(HELP if not behavior.get("old") else "old cli")
    sys.exit(0)
if args == ["login", "status"]:
    print(behavior.get("auth_text", "Logged in using API key: do-not-log-this" if behavior.get("api") else "Logged in using ChatGPT"), file=sys.stderr)
    sys.exit(0)
if args[0] != "exec":
    sys.exit(99)
assert "--full-auto" not in args and "--yolo" not in args
assert args[args.index("--sandbox") + 1] == "read-only"
assert "--ignore-user-config" in args and "--ignore-rules" in args
assert 'forced_login_method="chatgpt"' in args
assert not any(k in os.environ for k in ["OPENAI_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"])
text = sys.stdin.read()
Path(PROMPT).write_text(text)
print(json.dumps({"type":"thread.started", "thread_id":"fake-thread"}), flush=True)
if behavior.get("sleep"):
    subprocess.Popen([sys.executable, "-c", "import time; from pathlib import Path; time.sleep(3); Path(" + repr(MARKER) + ").write_text('leaked')"])
    time.sleep(20)
if behavior.get("failure"):
    print(json.dumps({"type":"turn.failed", "error":{"message":"quota exhausted"}}), flush=True)
    sys.exit(0 if behavior.get("zero_exit") else 1)
if behavior.get("modify"):
    Path(behavior["modify"]).write_text("changed by fake")
output = Path(args[args.index("--output-last-message") + 1])
output.write_text("not json" if behavior.get("invalid") else json.dumps(RESULT))
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":123, "output_tokens":45}}), flush=True)
'''


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.source / "README.md").write_text("# Original fixture\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial fixture")
        self.state = State(self.root / "state")
        self.addCleanup(self.state.close)
        self.behavior = self.root / "behavior.json"
        write_json(self.behavior, {})
        self.trace = self.root / "trace.jsonl"
        self.marker = self.root / "escaped-child"
        self.fake = self.root / "codex"
        constants = {"BEHAVIOR": str(self.behavior), "TRACE": str(self.trace),
                     "PROMPT": str(self.root / "sent-prompt"), "MARKER": str(self.marker),
                     "RESULT": RESULT, "HELP": " ".join(codex.REQUIRED_EXEC_FLAGS) + " --strict-config"}
        self.fake.write_text("#!" + sys.executable + "\n" + "\n".join(f"{k} = {v!r}" for k, v in constants.items()) + "\n" + FAKE_CODEX)
        self.fake.chmod(0o700)
        self.state.config = replace(self.state.config, codex_binary=str(self.fake), include_github=False)
        with self.state.lock():
            repo.add(self.state, str(self.source), "sdk", None)
        with self.state.db:
            self.state.db.execute("INSERT INTO maintainers VALUES (?,?,?)", ("mira", "sdk", cycle.MISSION))

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.source, stderr=subprocess.DEVNULL, text=True).strip()

    def wake(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return cycle.wake(self.state, "mira", **kwargs)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def executions(self):
        return [args for args in self.calls() if args[0] == "exec" and "--help" not in args]

    def latest(self):
        return self.state.rows("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1")[0]


class CycleTests(Fixture):
    def test_full_cycle_report_and_memory(self):
        run_id = self.wake()
        row = self.state.one("runs", run_id)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(json.loads(row["usage"])["usage"]["input_tokens"], 123)
        self.assertIn("Local report only", (self.state.home / "runs" / run_id / "report.md").read_text())
        self.assertFalse((self.state.home / "workspaces" / run_id).exists())
        self.assertEqual(len(self.state.memories("mira")["agent_observations"]), 1)
        self.assertEqual(len(self.executions()), 1)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_second_cycle_fresh_commit_and_memory(self):
        first = self.wake()
        (self.source / "README.md").write_text("# New committed content\n")
        self.git("add", ".")
        self.git("commit", "-m", "Fresh commit")
        second = self.wake()
        context = json.loads((self.state.home / "runs" / second / "context.json").read_text())
        self.assertEqual(context["previous_reports"][0]["id"], first)
        self.assertEqual(context["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(len(context["memory"]["agent_observations"]), 1)
        self.assertIn("Fresh commit", context["recent_commits"])

    def test_dry_run_never_invokes_codex_or_adds_memory(self):
        run_id = self.wake(dry_run=True)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state.one("runs", run_id)["status"], "dry_run")
        self.assertEqual(self.state.memories("mira")["agent_observations"], [])
        self.assertEqual(self.state.remaining(), 4)
        self.assertTrue((self.state.home / "runs" / run_id / "prompt.txt").is_file())

    def test_dry_run_without_installed_codex(self):
        self.state.config = replace(self.state.config, codex_binary="no-such-codex")
        self.wake(dry_run=True)

    def test_api_login_refused_without_changing_credentials(self):
        write_json(self.behavior, {"api": True})
        with self.assertRaisesRegex(Error, "subscription login"):
            self.wake()
        self.assertEqual(self.executions(), [])
        self.assertFalse(any("logout" in args for args in self.calls()))
        self.assertEqual(self.state.rows("SELECT * FROM runs"), [])

    def test_unrecognized_auth_status_is_refused(self):
        write_json(self.behavior, {"auth_text": "ChatGPT is available, but not logged in"})
        with self.assertRaisesRegex(Error, "subscription login"):
            self.wake()
        self.assertEqual(self.executions(), [])

    def test_old_cli_refused_no_insecure_fallback(self):
        write_json(self.behavior, {"old": True})
        with self.assertRaisesRegex(Error, "required safe-automation"):
            self.wake()
        self.assertEqual(self.executions(), [])

    def test_parent_secrets_not_passed_to_codex(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "never-use", "CODEX_API_KEY": "never-use",
                                     "GH_TOKEN": "never-use", "GITHUB_TOKEN": "never-use",
                                     "SSH_AUTH_SOCK": "/secret/ssh"}):
            self.wake()

    def test_malformed_json_is_failed_not_memory(self):
        write_json(self.behavior, {"invalid": True})
        with self.assertRaisesRegex(Error, "not valid JSON"):
            self.wake()
        self.assertEqual(self.latest()["status"], "failed")
        self.assertEqual(self.state.memories("mira")["agent_observations"], [])
        self.assertEqual(json.loads(self.latest()["usage"])["usage"]["input_tokens"], 123)

    def test_failure_event_with_exit_zero_is_still_failed(self):
        write_json(self.behavior, {"failure": True, "zero_exit": True})
        with self.assertRaisesRegex(Error, "Codex failed"):
            self.wake()
        self.assertEqual(self.latest()["status"], "failed")
        self.assertEqual(len(self.executions()), 1)

    def test_quota_failure_does_not_retry(self):
        write_json(self.behavior, {"failure": True})
        with self.assertRaises(Error):
            self.wake()
        self.assertEqual(len(self.executions()), 1)
        self.assertEqual(self.state.remaining(), 3)

    def test_timeout_stops_descendant_processes(self):
        self.state.config = replace(self.state.config, timeout_seconds=1)
        write_json(self.behavior, {"sleep": True})
        with self.assertRaisesRegex(Error, "exceeded"):
            self.wake()
        self.assertEqual(self.latest()["status"], "timed_out")
        time.sleep(3)
        self.assertFalse(self.marker.exists())
        self.assertEqual(len(self.executions()), 1)

    def test_integrity_failure_retains_worktree_and_rejects_memory(self):
        original = codex.execute
        def mutate(config, artifacts, prompt, lock_fd):
            write_json(self.behavior, {"modify": str(self.state.home / "workspaces" / artifacts.name / "README.md")})
            return original(config, artifacts, prompt, lock_fd)
        with patch("maintainerd.codex.execute", side_effect=mutate):
            with self.assertRaisesRegex(Error, "integrity check failed"):
                self.wake()
        row = self.latest()
        self.assertTrue((self.state.home / "workspaces" / row["id"] / "README.md").is_file())
        self.assertEqual(self.state.memories("mira")["agent_observations"], [])
        self.assertEqual((self.source / "README.md").read_text(), "# Original fixture\n")

    def test_interruption_recorded(self):
        with patch("maintainerd.codex.execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.wake()
        self.assertEqual(self.latest()["status"], "interrupted")
        self.assertEqual(self.state.memories("mira")["agent_observations"], [])

    def test_daily_budget_and_dry_run_exemption(self):
        self.state.config = replace(self.state.config, max_runs_per_day=1)
        self.wake()
        before = len(self.calls())
        with self.assertRaisesRegex(Error, "Daily run budget"):
            self.wake()
        self.assertEqual(len(self.calls()), before)
        self.wake(dry_run=True)
        self.assertEqual(self.state.remaining(), 0)

    def test_pause_blocks_real_but_not_dry_run(self):
        (self.state.home / "PAUSED").touch()
        with self.assertRaisesRegex(Error, "paused"):
            self.wake()
        self.assertEqual(self.calls(), [])
        self.wake(dry_run=True)

    def test_single_host_lock_prevents_second_run(self):
        with self.state.lock():
            with self.assertRaises(Busy):
                self.wake()
        self.assertEqual(self.calls(), [])

    def test_stale_run_marked_interrupted(self):
        with self.state.db:
            self.state.db.execute("INSERT INTO runs(id,maintainer,reason,status,started_at) VALUES (?,?,?,?,?)",
                                  ("stale", "mira", "exploration", "running", utcnow()))
        self.wake(dry_run=True)
        self.assertEqual(self.state.one("runs", "stale")["status"], "interrupted")

    def test_user_uncommitted_changes_untouched(self):
        (self.source / "README.md").write_text("Uncommitted user work\n")
        (self.source / "untracked").write_text("Leave this alone")
        before = self.git("status", "--porcelain")
        run_id = self.wake(dry_run=True, keep_worktree=True)
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual((self.state.home / "workspaces" / run_id / "README.md").read_text(), "# Original fixture\n")

    def test_prompt_does_not_treat_sdk_usage_as_development_task(self):
        run_id = self.wake(dry_run=True)
        prompt = (self.state.home / "runs" / run_id / "prompt.txt").read_text()
        self.assertIn("distinguish SDK development", prompt)
        self.assertIn("No permission is implied by silence", prompt)
        self.assertIn("No GitHub action is available", prompt)

    def test_cleanup_failure_retains_success_and_warning(self):
        with patch("maintainerd.repo.cleanup", side_effect=Error("test cleanup failure")):
            run_id = self.wake()
        self.assertEqual(self.latest()["status"], "completed")
        self.assertTrue((self.state.home / "runs" / run_id / "cleanup-warning.json").is_file())

    def test_repo_add_idempotent(self):
        self.assertEqual(repo.add(self.state, str(self.source), "sdk", None), "sdk")
        self.assertEqual(len(self.state.rows("SELECT * FROM repositories")), 1)

    def test_failed_add_does_not_leave_partial_clone(self):
        with self.assertRaises(Error):
            repo.add(self.state, str(self.source), "missing", "no-such-branch")
        self.assertFalse((self.state.home / "repos" / "missing.git").exists())

    def test_no_push_remote(self):
        value = repo.git(["--git-dir", str(self.state.home / "repos" / "sdk.git"),
                          "config", "remote.origin.pushurl"])
        self.assertIn("disabled://", value)

    def test_cli_create_memory_show(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.dispatch(cli.parser().parse_args(["memory", "add", "mira", "Prefer additive APIs"]), self.state), 0)
        self.assertEqual(self.state.memories("mira")["human_notes"][0]["body"], "Prefer additive APIs")
        run_id = self.wake()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            cli.dispatch(cli.parser().parse_args(["show", run_id, "--json"]), self.state)
        self.assertEqual(json.loads(output.getvalue())["status"], "completed")

    def test_doctor_does_not_run_inference(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("maintainerd.cli.shutil.which", side_effect=lambda value: str(self.fake) if value == str(self.fake) else None):
            self.assertEqual(cli.doctor(self.state), 0)
        self.assertEqual(self.executions(), [])

    def test_foreground_loop_stops_on_runtime_failure(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                patch("maintainerd.discussion.sync_once", return_value=0), \
                patch("maintainerd.cycle.wake", side_effect=Error("stop")) as wake:
            with self.assertRaisesRegex(Error, "stop"):
                cli.serve(self.state, "mira", 12, 300)
        self.assertEqual(wake.call_count, 1)


class ContractTests(unittest.TestCase):
    def test_valid_report(self):
        self.assertEqual(report.parse(json.dumps(RESULT)), RESULT)

    def test_unknown_report_field_refused(self):
        with self.assertRaises(Error):
            report.parse(json.dumps(dict(RESULT, command="git push")))

    def test_empty_propose_refused(self):
        with self.assertRaises(Error):
            report.parse(json.dumps(dict(RESULT, findings=[])))

    def test_no_action_is_valid(self):
        result = dict(RESULT, outcome="no_action", findings=[], memory_notes=[])
        self.assertEqual(report.parse(json.dumps(result))["outcome"], "no_action")

    def test_no_action_with_findings_refused(self):
        with self.assertRaises(Error):
            report.parse(json.dumps(dict(RESULT, outcome="no_action")))

    def test_missing_evidence_refused(self):
        result = dict(RESULT, findings=[dict(RESULT["findings"][0], evidence=[])])
        with self.assertRaises(Error):
            report.parse(json.dumps(result))

    def test_three_findings_refused(self):
        with self.assertRaises(Error):
            report.parse(json.dumps(dict(RESULT, findings=RESULT["findings"] * 3)))

    def test_wrong_type_refused(self):
        with self.assertRaises(Error):
            report.parse(json.dumps(dict(RESULT, memory_notes="not a list")))

    def test_unknown_outcome_refused(self):
        with self.assertRaises(Error):
            report.parse(json.dumps(dict(RESULT, outcome="opened_issue")))

    def test_malformed_report_refused(self):
        with self.assertRaises(Error):
            report.parse("```json\n{}\n```")

    def test_path_traversal_names_refused(self):
        for value in ("../x", "-x", "x/y", "x;touch", "", "a" * 65):
            with self.subTest(value=value), self.assertRaises(Error):
                name(value)

    def test_token_urls_refused(self):
        for value in ("https://secret@github.com/a/b", "https://github.com/a/b?token=secret", "ext::evil"):
            with self.subTest(value=value), self.assertRaises(Error):
                repo.source_info(value)

    def test_github_sources(self):
        for value in ("https://github.com/a/b.git", "git@github.com:a/b.git", "https://github.com/a/b"):
            self.assertEqual(repo.source_info(value)[1], "a/b")

    def test_safe_argv_and_no_model_pin(self):
        args = codex.argv(Config(), Path("/tmp/control"), Path("/tmp/artifacts"))
        self.assertNotIn("--model", args)
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn('web_search="disabled"', args)
        self.assertIn("features.apps=false", args)
        self.assertIn("features.hooks=false", args)
        self.assertIn("--ignore-user-config", args)
        self.assertEqual(args[-1], "-")

    def test_explicit_model(self):
        args = codex.argv(Config(model="test-model"), Path("/tmp/control"), Path("/tmp/artifacts"))
        self.assertEqual(args[args.index("--model") + 1], "test-model")

    def test_environment_allowlist(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "bad", "CODEX_API_KEY": "bad", "AWS_SECRET_ACCESS_KEY": "bad",
                                     "CODEX_HOME": "/existing-login", "SOME_RANDOM_SECRET": "bad"}):
            env = codex.environment()
        self.assertEqual(env["CODEX_HOME"], "/existing-login")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("SOME_RANDOM_SECRET", env)

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for value in ('api_key="secret"', 'max_runs_per_day=true', 'timeout_seconds=0',
                          'model=[]', 'include_github="yes"', 'codex_binary=""', 'bad syntax'):
                with self.subTest(value=value):
                    path.write_text(value)
                    with self.assertRaises(Error):
                        Config.load(path)

    def test_event_parser_tolerates_unknown_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events"
            path.write_text('not json\n[]\n{"type":"new.event"}\n'
                            '{"type":"turn.completed","usage":{"input_tokens":3}}\n'
                            '{"type":"turn.completed","usage":{"input_tokens":4,"bad":true}}\n')
            self.assertEqual(codex.events(path)["usage"], {"input_tokens": 7})


class GithubTests(unittest.TestCase):
    def test_missing_gh_is_explicit(self):
        with patch("maintainerd.github.shutil.which", return_value=None):
            data = github.snapshot("a/b", True)
        self.assertIn("not installed", data["limitations"][0])

    def test_disabled_makes_no_requests(self):
        with patch("maintainerd.github.command") as command:
            github.snapshot("a/b", False)
        command.assert_not_called()

    def test_only_get_requests_and_metadata(self):
        issue = {"number": 1, "title": "Idea", "body": "Discuss", "labels": [], "user": {"login": "human"}}
        def response(args, **_kwargs):
            self.assertEqual(args[args.index("--method") + 1], "GET")
            endpoint = args[-1]
            if "/comments?" in endpoint:
                return "[]"
            if "/actions/" in endpoint:
                return '{"workflow_runs":[]}'
            return json.dumps([issue])
        with patch("maintainerd.github.shutil.which", return_value="/gh"), patch("maintainerd.github.command", side_effect=response) as command:
            data = github.snapshot("a/b", True)
        self.assertEqual(data["open_items"][0]["title"], "Idea")
        self.assertEqual(command.call_count, 4)
        self.assertIn("inline reviews", " ".join(data["limitations"]))

    def test_api_failure_is_not_no_open_issues_claim(self):
        with patch("maintainerd.github.shutil.which", return_value="/gh"), patch("maintainerd.github.command", side_effect=Error("not authorized")):
            data = github.snapshot("a/b", True)
        self.assertIn("snapshot incomplete", data["limitations"][0])
        self.assertIn("unavailable", data["limitations"][1])

    def test_caps_and_truncation_are_disclosed(self):
        items = [{"number": i, "body": "a" * 12001, "labels": []} for i in range(100)]
        def response(args, **_kwargs):
            if "/comments?" in args[-1]:
                return json.dumps([{"body": "b" * 8001}] * 20)
            if "/actions/" in args[-1]:
                return '{"workflow_runs":[]}'
            return json.dumps(items)
        with patch("maintainerd.github.shutil.which", return_value="/gh"), patch("maintainerd.github.command", side_effect=response):
            data = github.snapshot("a/b", True)
        text = " ".join(data["limitations"])
        self.assertIn("capped at 100", text)
        self.assertIn("recent replies may be missing", text)
        self.assertIn("12000", text)
        self.assertIn("8000", text)


if __name__ == "__main__":
    unittest.main()
