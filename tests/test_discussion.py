from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintainerd import discussion, publisher, repo
from maintainerd.state import Error, State


REPLY = {
    "action": "reply",
    "summary": "The comment raises a useful design distinction.",
    "reply": "Atomic rejection is safer here because inherited loading lacks the ordinary-file diagnostic guard.",
    "progress": ["Connected inherited loading behavior to the existing diagnostic guard."],
    "inspected_paths": ["src/example.py"],
    "limitations": ["Tests were not run."],
    "memory_notes": ["Issue #11 discusses atomic rejection versus diagnostic-only loading."],
}
NO_REPLY = {
    "action": "no_reply",
    "summary": "The latest comment only repeats agreement.",
    "reply": "",
    "progress": [],
    "inspected_paths": [],
    "limitations": [],
    "memory_notes": [],
}


class DiscussionTests(unittest.TestCase):
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
        subprocess.check_call(["git", "commit", "-m", "fixture"], cwd=self.source, stdout=subprocess.DEVNULL)

        self.state = State(self.root / "state")
        with self.state.lock():
            repo.add(self.state, str(self.source), "sdk", None, "owner/repo")
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO maintainers(name,repository,mission) VALUES (?,?,?)",
                ("mira", "sdk", "Improve it thoughtfully."),
            )
            self.state.db.execute(
                "INSERT INTO runs(id,maintainer,reason,status,started_at,finished_at,commit_sha,result) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("origin-run", "mira", "exploration", "completed", "2026-01-01", "2026-01-01",
                 "abc", json.dumps({"outcome":"no_action","summary":"x","findings":[],
                                    "inspected_paths":[],"limitations":[],"memory_notes":[]})),
            )
            self.state.db.execute(
                "INSERT INTO proposal_routes(run_id,finding_index,repository,issue_number,issue_url,title,mode,published_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("origin-run", 0, "owner/repo", 11, "https://github.com/owner/repo/issues/11",
                 "Proposal", "created", "2026-01-01"),
            )

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def thread(self, comments):
        return {
            "issue": {
                "number": 11,
                "title": "Proposal",
                "state": "open",
                "html_url": "https://github.com/owner/repo/issues/11",
                "body": "Discuss a design choice.",
                "user": {"login": "mira-maintains[bot]", "type": "Bot"},
            },
            "comments": comments,
        }

    def test_contract_requires_progress_for_reply(self):
        self.assertEqual(discussion.parse(json.dumps(REPLY))["action"], "reply")
        bad = dict(REPLY, progress=[])
        with self.assertRaisesRegex(Error, "concrete progress"):
            discussion.parse(json.dumps(bad))
        self.assertEqual(discussion.parse(json.dumps(NO_REPLY))["action"], "no_reply")

    def test_bot_comments_are_inputs_but_self_comments_are_ignored(self):
        active = publisher.Session("owner/repo", "token", "mira-maintains[bot]", {"issues":"write"})
        comments = [
            {"id": 1, "body": "my own prior reply", "created_at": "2026-01-01",
             "user": {"login": "mira-maintains[bot]", "type": "Bot"}},
            {"id": 2, "body": "I disagree because of parser semantics", "created_at": "2026-01-02",
             "user": {"login": "noah-maintains[bot]", "type": "Bot"}},
        ]
        inserted = discussion._record_comments(
            self.state, "mira", "owner/repo", 11, comments, active.bot_login
        )
        self.assertEqual(inserted, 1)
        rows = self.state.rows("SELECT comment_id,status,author_type FROM thread_events ORDER BY comment_id")
        self.assertEqual(rows[0]["status"], "ignored")
        self.assertEqual(rows[1]["status"], "pending")
        self.assertEqual(rows[1]["author_type"], "Bot")

    def test_same_external_comment_can_be_seen_by_two_distinct_maintainers(self):
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO maintainers(name,repository,mission) VALUES (?,?,?)",
                ("noah", "sdk", "Improve it independently."),
            )
        comments = [{
            "id": 77,
            "body": "A shared engineering observation.",
            "created_at": "2026-01-02",
            "user": {"login": "third-maintainer[bot]", "type": "Bot"},
        }]
        self.assertEqual(
            discussion._record_comments(
                self.state, "mira", "owner/repo", 11, comments, "mira-maintains[bot]"
            ),
            1,
        )
        self.assertEqual(
            discussion._record_comments(
                self.state, "noah", "owner/repo", 11, comments, "noah-maintains[bot]"
            ),
            1,
        )
        rows = self.state.rows(
            "SELECT maintainer,status FROM thread_events WHERE comment_id=77 ORDER BY maintainer"
        )
        self.assertEqual([row["maintainer"] for row in rows], ["mira", "noah"])

    @patch("maintainerd.discussion.implementation.run_step")
    @patch("maintainerd.discussion.implementation.authorize")
    @patch("maintainerd.discussion.publisher.issue_thread")
    @patch("maintainerd.discussion.publisher.session_for")
    def test_historical_approval_preempts_another_discussion_turn(
        self, session_for, issue_thread, authorize, run_step
    ):
        active = publisher.Session(
            "owner/repo",
            "token",
            "mira-maintains[bot]",
            {"issues":"write","contents":"write","pull_requests":"write"},
        )
        session_for.return_value = active
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO thread_events("
                "maintainer,repository,issue_number,comment_id,author,author_type,body,created_at,"
                "status,processed_at,turn_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "mira", "owner/repo", 11, 400, "owner", "User",
                    "sounds good. you may create a PR with option 1. thanks mira!",
                    "2026-01-02", "processed", "2026-01-02", "thread:old",
                ),
            )
        issue_thread.return_value = self.thread([])
        authorize.return_value = {
            "id": 7,
            "repository": "owner/repo",
            "issue_number": 11,
            "pr_number": 14,
        }

        with patch("maintainerd.discussion.respond") as respond, \
                contextlib.redirect_stdout(io.StringIO()):
            processed = discussion.sync_once(self.state, "mira")

        self.assertEqual(processed, 2)
        history = authorize.call_args.args[-1]
        self.assertEqual(history[0]["comment_id"], 400)
        run_step.assert_called_once()
        respond.assert_not_called()

    @patch("maintainerd.discussion.review.sync_peer_once", return_value=False)
    @patch("maintainerd.discussion.implementation.continue_one", return_value=False)
    @patch("maintainerd.discussion.respond")
    @patch("maintainerd.discussion.publisher.issue_thread")
    @patch("maintainerd.discussion.publisher.session_for")
    def test_peer_created_issue_is_visible_to_other_maintainers(
        self, session_for, issue_thread, respond, continue_one, peer_review
    ):
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO maintainers(name,repository,mission) VALUES (?,?,?)",
                ("noah", "sdk", "Improve it independently."),
            )
            self.state.db.execute(
                "INSERT INTO runs(id,maintainer,reason,status,started_at,finished_at,commit_sha,result) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "noah-run", "noah", "exploration", "completed",
                    "2026-01-02", "2026-01-02", "abc",
                    json.dumps({"outcome":"no_action","summary":"x","findings":[],
                                "inspected_paths":[],"limitations":[],"memory_notes":[]}),
                ),
            )
            self.state.db.execute(
                "INSERT INTO proposal_routes("
                "run_id,finding_index,repository,issue_number,issue_url,title,mode,published_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (
                    "noah-run", 0, "owner/repo", 12,
                    "https://github.com/owner/repo/issues/12",
                    "Peer proposal", "created", "2026-01-02",
                ),
            )
        active = publisher.Session(
            "owner/repo", "token", "mira-maintains[bot]", {"issues":"write"}
        )
        session_for.return_value = active
        issue_thread.side_effect = lambda active, number: {
            "issue": {
                "number": number,
                "title": "Peer proposal" if number == 12 else "Proposal",
                "state": "open",
                "html_url": f"https://github.com/owner/repo/issues/{number}",
                "body": "A concrete proposal from another maintainer.",
                "created_at": "2026-01-02",
                "user": {
                    "login": "noah-maintains[bot]" if number == 12 else "mira-maintains[bot]",
                    "type": "Bot",
                },
            },
            "comments": [],
        }

        with contextlib.redirect_stdout(io.StringIO()):
            discussion.sync_once(self.state, "mira")

        calls = [call for call in respond.call_args_list if call.args[4] == 12]
        self.assertEqual(len(calls), 1)
        trigger = calls[0].args[5][0]
        self.assertEqual(trigger["comment_id"], -12)
        self.assertEqual(trigger["author"], "noah-maintains[bot]")

    @patch("maintainerd.discussion.codex.preflight", return_value="codex-test")
    @patch("maintainerd.discussion.codex.execute")
    @patch("maintainerd.discussion.publisher.post_comment")
    @patch("maintainerd.discussion.publisher.issue_thread")
    @patch("maintainerd.discussion.publisher.session")
    def test_sync_wakes_on_human_comment_and_posts_validated_reply(
        self, session, issue_thread, post_comment, execute, preflight
    ):
        active = publisher.Session("owner/repo", "token", "mira-maintains[bot]", {"issues":"write"})
        session.return_value = active
        comments = [{
            "id": 100,
            "body": "Why not diagnostic-only loading?",
            "created_at": "2026-01-02T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "html_url": "https://github.com/owner/repo/issues/11#issuecomment-100",
            "user": {"login": "human", "type": "User"},
        }]
        issue_thread.return_value = self.thread(comments)
        post_comment.return_value = {
            "id": 101,
            "html_url": "https://github.com/owner/repo/issues/11#issuecomment-101",
        }

        def fake_execute(config, artifacts, prompt, lock_fd):
            (artifacts / "result.json").write_text(json.dumps(REPLY))
            (artifacts / "events.jsonl").write_text(
                json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}) + "\n"
            )
            return {}
        execute.side_effect = fake_execute

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            processed = discussion.sync_once(self.state, "mira")
        self.assertEqual(processed, 1)
        post_comment.assert_called_once()
        body = post_comment.call_args.args[2]
        self.assertIn("maintainerd turn=", body)
        self.assertIn("Atomic rejection", body)
        event = self.state.rows("SELECT * FROM thread_events WHERE comment_id=100")[0]
        self.assertEqual(event["status"], "processed")
        turn = self.state.rows("SELECT * FROM thread_turns")[0]
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(turn["reply_comment_id"], 101)
        prompt = (self.state.home / "threads" / turn["id"] / "prompt.txt").read_text()
        self.assertIn("Bot-to-bot", prompt)
        self.assertEqual(self.state.remaining(), 3)

    @patch("maintainerd.discussion.publisher.issue_thread")
    @patch("maintainerd.discussion.publisher.session")
    def test_self_comment_does_not_spend_model_run(self, session, issue_thread):
        session.return_value = publisher.Session("owner/repo", "token", "mira-maintains[bot]", {"issues":"write"})
        issue_thread.return_value = self.thread([{
            "id": 200,
            "body": "self",
            "created_at": "2026-01-02",
            "user": {"login": "mira-maintains[bot]", "type": "Bot"},
        }])
        with patch("maintainerd.discussion.respond") as respond:
            processed = discussion.sync_once(self.state, "mira")
        self.assertEqual(processed, 0)
        respond.assert_not_called()
        self.assertEqual(self.state.remaining(), 4)

    @patch("maintainerd.discussion.codex.preflight", return_value="codex-test")
    @patch("maintainerd.discussion.codex.execute")
    @patch("maintainerd.discussion.publisher.post_comment")
    @patch("maintainerd.discussion.publisher.issue_thread")
    @patch("maintainerd.discussion.publisher.session")
    def test_no_reply_marks_comment_processed_without_posting(
        self, session, issue_thread, post_comment, execute, preflight
    ):
        session.return_value = publisher.Session("owner/repo", "token", "mira-maintains[bot]", {"issues":"write"})
        issue_thread.return_value = self.thread([{
            "id": 300,
            "body": "Sounds good.",
            "created_at": "2026-01-02",
            "user": {"login": "human", "type": "User"},
        }])

        def fake_execute(config, artifacts, prompt, lock_fd):
            (artifacts / "result.json").write_text(json.dumps(NO_REPLY))
        execute.side_effect = fake_execute
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(discussion.sync_once(self.state, "mira"), 1)
        post_comment.assert_not_called()
        self.assertEqual(
            self.state.rows("SELECT status FROM thread_events WHERE comment_id=300")[0]["status"],
            "processed",
        )


if __name__ == "__main__":
    unittest.main()
