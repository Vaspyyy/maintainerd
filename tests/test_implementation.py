from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintainerd import implementation, publisher, repo
from maintainerd.state import Error, State


class ImplementationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.check_call(["git", "init", "-b", "main"], cwd=self.source, stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "config", "user.name", "Test"], cwd=self.source)
        subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=self.source)
        (self.source / "src").mkdir()
        (self.source / "src/example.py").write_text("VALUE = 1\n")
        subprocess.check_call(["git", "add", "."], cwd=self.source)
        subprocess.check_call(["git", "commit", "-m", "base"], cwd=self.source, stdout=subprocess.DEVNULL)
        self.base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.source, text=True
        ).strip()

        self.state = State(self.root / "state")
        with self.state.lock():
            repo.add(self.state, str(self.source), "sdk", None, "owner/repo")
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO maintainers(name,repository,mission) VALUES (?,?,?)",
                ("mira", "sdk", "Improve it thoughtfully."),
            )
        self.maintainer = self.state.one("maintainers", "mira")
        self.repository = self.state.one("repositories", "sdk")
        self.active = publisher.Session(
            "owner/repo",
            "token",
            "mira-maintains[bot]",
            {"issues": "write", "contents": "write", "pull_requests": "write"},
        )

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def approval(self):
        return {"id": 1, "comment_id": 55, "author": "owner", "author_type": "User",
                "body": "implement it"}

    def test_design_agreement_is_not_implementation_approval(self):
        self.assertFalse(implementation.explicit_approval("option 1 sounds good."))
        for text in (
            "implement it",
            "/implement option 1",
            "go ahead and implement option 1",
            "please implement this",
            "make the PR",
        ):
            with self.subTest(text=text):
                self.assertTrue(implementation.explicit_approval(text))

    def test_only_repository_owner_can_authorize(self):
        events = [
            {"author": "other", "author_type": "User", "body": "implement it"},
            {"author": "owner", "author_type": "Bot", "body": "implement it"},
        ]
        self.assertIsNone(implementation.approval_event(events, "owner/repo"))
        events.append(
            {"author": "owner", "author_type": "User", "body": "go ahead and implement option 1"}
        )
        self.assertEqual(
            implementation.approval_event(events, "owner/repo")["author"], "owner"
        )

    @patch("maintainerd.implementation.publisher.create_draft_pr")
    @patch("maintainerd.implementation.publisher.issue_thread")
    @patch("maintainerd.implementation.publisher.pulls_for_head", return_value=[])
    @patch("maintainerd.implementation.publisher.create_ref")
    @patch("maintainerd.implementation.publisher.create_empty_commit")
    @patch("maintainerd.implementation.publisher.get_ref")
    def test_claim_branch_then_draft_pr_before_any_writable_turn(
        self, get_ref, create_empty, create_ref, pulls, issue_thread, create_pr
    ):
        get_ref.side_effect = [
            None,
            {"object": {"sha": self.base_sha}},
        ]
        create_empty.return_value = {"sha": "claim-sha"}
        issue_thread.return_value = {
            "issue": {"title": "Fix duplicate focuses"},
            "comments": [],
        }
        create_pr.return_value = {
            "number": 12,
            "html_url": "https://github.com/owner/repo/pull/12",
            "draft": True,
        }
        row = implementation.ensure_draft(
            self.state, self.maintainer, self.repository, self.active, 11, self.approval()
        )
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["branch"], "maintainerd/issue-11")
        self.assertEqual(row["pr_number"], 12)
        create_ref.assert_called_once()
        create_pr.assert_called_once()
        body = create_pr.call_args.kwargs["body"]
        self.assertIn("before implementation source edits", body.lower())

    @patch("maintainerd.implementation.publisher.get_git_commit")
    @patch("maintainerd.implementation.publisher.get_ref")
    def test_losing_remote_claim_never_invents_an_alternate_branch(self, get_ref, get_commit):
        get_ref.return_value = {"object": {"sha": "someone-elses-claim"}}
        get_commit.return_value = {
            "message": (
                "chore: claim #11\n\n"
                "Maintainerd-Issue: 11\n"
                "Maintainerd-Maintainer: noah\n"
                "Maintainerd-Claim: other\n"
            )
        }
        with self.assertRaisesRegex(Error, "already claimed"):
            implementation.ensure_draft(
                self.state, self.maintainer, self.repository, self.active, 11, self.approval()
            )
        self.assertEqual(
            self.state.rows("SELECT * FROM implementations"), []
        )

    def _insert_implementation(self):
        now = "2026-01-01T00:00:00+00:00"
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO implementations("
                "maintainer,repository,issue_number,branch,status,claim_id,claim_commit_sha,"
                "pr_number,pr_url,approval_comment_id,approved_by,base_sha,started_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "mira", "owner/repo", 11, "maintainerd/issue-11", "draft",
                    "claim", "claimsha", 12, "https://github.com/owner/repo/pull/12",
                    55, "owner", self.base_sha, now, now,
                ),
            )
        return self.state.rows("SELECT * FROM implementations")[0]

    @patch("maintainerd.implementation.codex.execute")
    @patch("maintainerd.implementation.publisher.pull_request")
    def test_non_draft_pr_blocks_writable_codex(self, pull_request, execute):
        impl = self._insert_implementation()
        pull_request.return_value = {"state": "open", "draft": False}
        with self.assertRaisesRegex(Error, "no longer a draft"):
            implementation.run_step(
                self.state, self.maintainer, self.repository, self.active, impl, 1
            )
        execute.assert_not_called()

    @patch("maintainerd.implementation.repo.push_head")
    @patch("maintainerd.implementation.publisher.issue_thread")
    @patch("maintainerd.implementation.publisher.pull_request")
    @patch("maintainerd.implementation.codex.preflight", return_value="codex-test")
    @patch("maintainerd.implementation.codex.execute")
    def test_one_writable_turn_produces_one_visible_commit(
        self, execute, preflight, pull_request, issue_thread, push_head
    ):
        subprocess.check_call(
            ["git", "branch", "maintainerd/issue-11"], cwd=self.source
        )
        impl = self._insert_implementation()
        pull_request.return_value = {
            "state": "open",
            "draft": True,
            "number": 12,
            "html_url": "https://github.com/owner/repo/pull/12",
        }
        issue_thread.return_value = {
            "issue": {"number": 11, "title": "Fix it", "body": "approved"},
            "comments": [],
        }

        def fake_execute(config, artifacts, prompt, lock_fd, **kwargs):
            self.assertEqual(kwargs["sandbox"], "workspace-write")
            workspace = kwargs["workspace"]
            (workspace / "src/example.py").write_text("VALUE = 2\n")
            (artifacts / "result.json").write_text(json.dumps({
                "action": "commit",
                "summary": "Implement the first focused change.",
                "commit_message": "fix: preserve inherited focus definitions",
                "tests_run": ["python -m unittest tests.test_example"],
                "limitations": [],
                "memory_notes": [],
            }))
            (artifacts / "events.jsonl").write_text(
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}) + "\n"
            )
            return {}
        execute.side_effect = fake_execute

        implementation.run_step(
            self.state, self.maintainer, self.repository, self.active, impl, 1
        )
        push_head.assert_called_once()
        step = self.state.rows("SELECT * FROM implementation_steps")[0]
        self.assertEqual(step["status"], "completed")
        self.assertTrue(step["commit_sha"])
        current = self.state.rows("SELECT * FROM implementations")[0]
        self.assertEqual(current["status"], "working")
        self.assertEqual(self.state.remaining(), 3)

    def test_step_contract_requires_commit_message_only_for_commit(self):
        good = {
            "action": "commit",
            "summary": "x",
            "commit_message": "fix: one thing",
            "tests_run": [],
            "limitations": [],
            "memory_notes": [],
        }
        self.assertEqual(implementation.parse_step(json.dumps(good))["action"], "commit")
        with self.assertRaises(Error):
            implementation.parse_step(json.dumps({**good, "commit_message": ""}))
        done = {**good, "action": "done", "commit_message": ""}
        self.assertEqual(implementation.parse_step(json.dumps(done))["action"], "done")


if __name__ == "__main__":
    unittest.main()
