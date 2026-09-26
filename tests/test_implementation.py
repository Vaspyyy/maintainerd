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
            "sounds good. you may create a PR with option 1. thanks mira!",
            "feel free to open a PR with option 1",
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

    def test_processed_owner_approval_is_recoverable_from_history(self):
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO thread_events("
                "maintainer,repository,issue_number,comment_id,author,author_type,body,created_at,"
                "status,processed_at,turn_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "mira", "owner/repo", 15, 5845302552, "owner", "User",
                    "sounds good. you may create a PR with option 1. thanks mira!",
                    "2026-01-02", "processed", "2026-01-02", "thread:old",
                ),
            )
        history = implementation.approval_history(
            self.state, "mira", "owner/repo", 15
        )
        self.assertEqual(len(history), 1)
        approval = implementation.approval_event(history, "owner/repo")
        self.assertIsNotNone(approval)
        self.assertEqual(approval["comment_id"], 5845302552)

    @patch("maintainerd.implementation.publisher.update_pull_request")
    @patch("maintainerd.implementation.publisher.create_draft_pr")
    @patch("maintainerd.implementation.publisher.issue_thread")
    @patch("maintainerd.implementation.publisher.pulls_for_head", return_value=[])
    @patch("maintainerd.implementation.publisher.create_ref")
    @patch("maintainerd.implementation.publisher.create_empty_commit")
    @patch("maintainerd.implementation.publisher.get_ref")
    def test_claim_branch_then_draft_pr_before_any_writable_turn(
        self, get_ref, create_empty, create_ref, pulls, issue_thread, create_pr, update_pr
    ):
        get_ref.side_effect = [
            None,
            {"object": {"sha": self.base_sha}},
        ]
        create_empty.return_value = {"sha": "claim-sha"}
        issue_thread.return_value = {
            "issue": {
                "title": "Fix duplicate focuses",
                "body": (
                    "## Problem\n\nDuplicates can silently lose content.\n\n"
                    "## Possible direction\n\nReject duplicates atomically before install.\n"
                ),
            },
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
        self.assertEqual(create_pr.call_args.kwargs["title"], "Fix duplicate focuses")
        body = create_pr.call_args.kwargs["body"]
        self.assertIn("## Summary", body)
        self.assertIn("## Problem", body)
        self.assertIn("Duplicates can silently lose content.", body)
        self.assertIn("## Approved direction", body)
        self.assertIn("Reject duplicates atomically before install.", body)
        self.assertIn("## Validation", body)
        self.assertIn("before implementation source edits", body.lower())
        update_pr.assert_called_once()

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

    @patch("maintainerd.implementation.ensure_draft")
    def test_losing_parallel_claim_is_coordination_not_runtime_failure(self, ensure_draft):
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO thread_events("
                "maintainer,repository,issue_number,comment_id,author,author_type,body,created_at,status"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                ("mira", "owner/repo", 11, 501, "owner", "User", "implement it", "2026-01-02", "pending"),
            )
        event = self.state.rows("SELECT * FROM thread_events WHERE comment_id=501")[0]
        ensure_draft.side_effect = implementation.ClaimLost(
            "Issue #11 is already claimed on maintainerd/issue-11 by noah."
        )
        self.assertIsNone(
            implementation.authorize(
                self.state, self.maintainer, self.repository, self.active, 11, [event]
            )
        )
        row = self.state.rows("SELECT * FROM thread_events WHERE comment_id=501")[0]
        self.assertEqual(row["status"], "processed")
        self.assertEqual(row["turn_id"], "implementation:claimed-elsewhere")
        self.assertEqual(
            implementation.approval_history(self.state, "mira", "owner/repo", 11),
            [],
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

    @patch("maintainerd.implementation.publisher.update_pull_request")
    @patch("maintainerd.implementation.repo.push_head")
    @patch("maintainerd.implementation.publisher.issue_thread")
    @patch("maintainerd.implementation.publisher.pull_request")
    @patch("maintainerd.implementation.codex.preflight", return_value="codex-test")
    @patch("maintainerd.implementation.codex.execute")
    def test_one_writable_turn_produces_one_visible_commit(
        self, execute, preflight, pull_request, issue_thread, push_head, update_pr
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
            "issue": {
                "number": 11,
                "title": "Fix it",
                "body": (
                    "## Problem\n\nBroken behavior.\n\n"
                    "## Possible direction\n\nFix it atomically.\n"
                ),
            },
            "comments": [],
        }

        def fake_execute(config, artifacts, prompt, lock_fd, **kwargs):
            self.assertEqual(kwargs["sandbox"], "workspace-write")
            workspace = kwargs["workspace"]
            (workspace / "src/example.py").write_text("VALUE = 2\n")
            (artifacts / "result.json").write_text(json.dumps({
                "action": "commit",
                "complete": False,
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
        update_pr.assert_called_once()
        refreshed_body = update_pr.call_args.kwargs["body"]
        self.assertIn("## Implementation progress", refreshed_body)
        self.assertIn("Implement the first focused change.", refreshed_body)
        self.assertIn("python -m unittest tests.test_example", refreshed_body)

    @patch("maintainerd.implementation.publisher.update_pull_request")
    @patch("maintainerd.implementation.publisher.mark_ready_for_review")
    @patch("maintainerd.implementation.repo.push_head")
    @patch("maintainerd.implementation.publisher.issue_thread")
    @patch("maintainerd.implementation.publisher.pull_request")
    @patch("maintainerd.implementation.codex.preflight", return_value="codex-test")
    @patch("maintainerd.implementation.codex.execute")
    def test_final_commit_can_complete_without_second_model_turn(
        self, execute, preflight, pull_request, issue_thread, push_head, mark_ready, update_pr
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
            "issue": {
                "number": 11,
                "title": "Fix it",
                "body": "## Problem\n\nBroken.\n\n## Possible direction\n\nFix it.\n",
            },
            "comments": [],
        }

        def fake_execute(config, artifacts, prompt, lock_fd, **kwargs):
            workspace = kwargs["workspace"]
            (workspace / "src/example.py").write_text("VALUE = 3\n")
            (artifacts / "result.json").write_text(json.dumps({
                "action": "commit",
                "complete": True,
                "summary": "Finish the approved fix and validate it.",
                "commit_message": "fix: finish approved behavior",
                "tests_run": ["pytest -q"],
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
        current = self.state.rows("SELECT * FROM implementations")[0]
        self.assertEqual(current["status"], "complete")
        push_head.assert_called_once()
        mark_ready.assert_called_once_with(self.active, 12)
        self.assertIn("ready for review", update_pr.call_args.kwargs["body"])

    @patch("maintainerd.implementation._sync_revision_requests")
    @patch("maintainerd.implementation.publisher.mark_ready_for_review")
    @patch("maintainerd.implementation.publisher.pull_request")
    @patch("maintainerd.implementation.publisher.session_for")
    @patch("maintainerd.implementation._refresh_pr_description")
    def test_complete_draft_is_reconciled_to_ready_without_model_turn(
        self, refresh, session_for, pull_request, mark_ready, sync_revisions
    ):
        impl = self._insert_implementation()
        with self.state.db:
            self.state.db.execute(
                "UPDATE implementations SET status='complete' WHERE id=?", (impl["id"],)
            )
        session_for.return_value = self.active
        pull_request.return_value = {"state": "open", "draft": True}
        sync_revisions.side_effect = lambda state, active, implementation: implementation

        with patch("maintainerd.implementation.run_step") as run_step:
            self.assertFalse(implementation.continue_one(self.state, "mira", 1))

        refresh.assert_called_once()
        mark_ready.assert_called_once_with(self.active, 12)
        run_step.assert_not_called()

    @patch("maintainerd.implementation.publisher.require_implementation_permissions")
    @patch("maintainerd.implementation.publisher.session_for")
    @patch("maintainerd.implementation.run_step")
    def test_unlimited_local_cap_still_continues_active_implementation(
        self, run_step, session_for, require_permissions
    ):
        impl = self._insert_implementation()
        self.state.config = self.state.config.__class__(
            **{**self.state.config.__dict__, "max_runs_per_day": 0}
        )
        session_for.return_value = self.active
        self.assertTrue(implementation.continue_one(self.state, "mira", 1))
        run_step.assert_called_once()
        require_permissions.assert_called_once_with(self.active)

    @patch("maintainerd.implementation.publisher.convert_to_draft")
    @patch("maintainerd.implementation.github.ci_for_head")
    @patch("maintainerd.implementation.publisher.pull_reviews")
    @patch("maintainerd.implementation.publisher.pull_review_comments", return_value=[])
    @patch("maintainerd.implementation.publisher.pull_request")
    def test_changes_requested_reopens_complete_pr_for_revision(
        self, pull_request, pull_review_comments, pull_reviews, ci_for_head, convert_to_draft
    ):
        impl = self._insert_implementation()
        with self.state.db:
            self.state.db.execute(
                "UPDATE implementations SET status='complete' WHERE id=?", (impl["id"],)
            )
        impl = self.state.rows("SELECT * FROM implementations WHERE id=?", (impl["id"],))[0]
        pull_request.return_value = {
            "state": "open",
            "draft": False,
            "head": {"sha": "abc123"},
        }
        pull_reviews.return_value = [{
            "id": 77,
            "state": "CHANGES_REQUESTED",
            "commit_id": "abc123",
            "body": "Inserted air wings can disappear. Add a regression and fix source identity.",
            "user": {"login": "owner"},
        }]
        ci_for_head.return_value = {"failed": [], "checks": [], "limitations": []}

        current = implementation._sync_revision_requests(
            self.state, self.active, impl
        )
        self.assertEqual(current["status"], "revision")
        convert_to_draft.assert_called_once_with(self.active, 12)
        request = self.state.rows("SELECT * FROM revision_requests")[0]
        self.assertEqual(request["status"], "pending")
        self.assertEqual(request["review_id"], 77)
        self.assertIn("air wings", request["body"])

    @patch("maintainerd.implementation.publisher.convert_to_draft")
    @patch("maintainerd.implementation.github.ci_for_head")
    @patch("maintainerd.implementation.publisher.pull_reviews", return_value=[])
    @patch("maintainerd.implementation.publisher.pull_review_comments", return_value=[])
    @patch("maintainerd.implementation.publisher.pull_request")
    def test_failed_ci_reopens_complete_pr_for_revision(
        self, pull_request, pull_review_comments, pull_reviews, ci_for_head, convert_to_draft
    ):
        impl = self._insert_implementation()
        with self.state.db:
            self.state.db.execute(
                "UPDATE implementations SET status='complete' WHERE id=?", (impl["id"],)
            )
        impl = self.state.rows("SELECT * FROM implementations WHERE id=?", (impl["id"],))[0]
        pull_request.return_value = {
            "state": "open",
            "draft": False,
            "head": {"sha": "abcdef1234567890"},
        }
        ci_for_head.return_value = {
            "checks": [{"name": "quality", "conclusion": "failure"}],
            "failed": [{"name": "quality", "conclusion": "failure"}],
            "limitations": [],
        }
        current = implementation._sync_revision_requests(
            self.state, self.active, impl
        )
        self.assertEqual(current["status"], "revision")
        convert_to_draft.assert_called_once()
        request = self.state.rows("SELECT * FROM revision_requests")[0]
        self.assertEqual(request["author"], "ci")
        self.assertIn("quality", request["body"])

    def test_pr_body_hides_coordination_plumbing_below_engineering_context(self):
        issue = {
            "title": "Reject duplicate focus IDs",
            "body": (
                "## Problem\n\nSaving can drop content.\n\n"
                "## Possible direction\n\nReject before installation.\n"
            ),
        }
        body = implementation._pr_body(
            11,
            "mira",
            "claim",
            "maintainerd/issue-11",
            issue,
            self.approval(),
            steps=[{
                "commit_sha": "abcdef0123456789",
                "result": json.dumps({
                    "action": "commit",
                    "complete": True,
                    "summary": "Reject duplicates before installing trees.",
                    "commit_message": "fix: reject duplicates",
                    "tests_run": ["pytest tests/test_focus.py"],
                    "limitations": [],
                    "memory_notes": [],
                }),
            }],
            status="working",
        )
        self.assertLess(body.index("## Summary"), body.index("maintainerd coordination metadata"))
        self.assertIn("Saving can drop content.", body)
        self.assertIn("Reject duplicates before installing trees.", body)
        self.assertIn("pytest tests/test_focus.py", body)

    def test_step_contract_requires_commit_message_only_for_commit(self):
        good = {
            "action": "commit",
            "complete": False,
            "summary": "x",
            "commit_message": "fix: one thing",
            "tests_run": [],
            "limitations": [],
            "memory_notes": [],
        }
        self.assertEqual(implementation.parse_step(json.dumps(good))["action"], "commit")
        with self.assertRaises(Error):
            implementation.parse_step(json.dumps({**good, "commit_message": ""}))
        done = {**good, "action": "done", "complete": True, "commit_message": ""}
        self.assertEqual(implementation.parse_step(json.dumps(done))["action"], "done")
        with self.assertRaises(Error):
            implementation.parse_step(json.dumps({**done, "complete": False}))


if __name__ == "__main__":
    unittest.main()
