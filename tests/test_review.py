from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintainerd import publisher, review
from maintainerd.state import Error, State


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"
        self.state = State(self.home)
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO repositories(name,source,branch,github) VALUES (?,?,?,?)",
                ("sdk", "unused", "main", "owner/repo"),
            )
            self.state.db.executemany(
                "INSERT INTO maintainers(name,repository,mission) VALUES (?,?,?)",
                [
                    ("mira", "sdk", "Improve it"),
                    ("noah", "sdk", "Improve it"),
                ],
            )
            self.state.db.execute(
                "INSERT INTO implementations("
                "maintainer,repository,issue_number,branch,status,claim_id,claim_commit_sha,"
                "pr_number,pr_url,approval_comment_id,approved_by,base_sha,started_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "noah", "owner/repo", 23, "maintainerd/issue-23", "complete",
                    "claim", "claimsha", 31, "https://github.com/owner/repo/pull/31",
                    1, "owner", "base", "2026-01-01", "2026-01-02",
                ),
            )
        self.repository = self.state.one("repositories", "sdk")
        self.active = publisher.Session(
            "owner/repo", "token", "mira-maintains[bot]",
            {"issues": "write", "contents": "write", "pull_requests": "write"},
        )

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_review_contract_requires_concrete_blockers(self):
        good = {
            "action": "request_changes",
            "summary": "Insertion can lose a wing.",
            "body": "A new wing can disappear when source identities collide.",
            "findings": ["source_index collision drops the inserted wing"],
            "inspected_paths": ["src/hoi4/oob.py"],
            "limitations": [],
            "memory_notes": [],
        }
        self.assertEqual(review.parse(json.dumps(good))["action"], "request_changes")
        with self.assertRaises(Error):
            review.parse(json.dumps({**good, "findings": []}))
        no_review = {
            **good,
            "action": "no_review",
            "body": "",
            "findings": [],
        }
        self.assertEqual(review.parse(json.dumps(no_review))["action"], "no_review")

    @patch("maintainerd.review.review_one")
    @patch("maintainerd.review.publisher.pull_reviews", return_value=[])
    @patch("maintainerd.review.publisher.pull_request")
    def test_ready_pr_from_other_maintainer_gets_review_opportunity(
        self, pull_request, pull_reviews, review_one
    ):
        pull_request.return_value = {
            "state": "open", "draft": False, "head": {"sha": "abc123"}
        }
        review_one.return_value = "turn1"
        self.assertTrue(
            review.sync_peer_once(
                self.state, "mira", self.repository, self.active, 1
            )
        )
        review_one.assert_called_once()
        self.assertEqual(review_one.call_args.args[4]["pr_number"], 31)

    @patch("maintainerd.review.review_one")
    @patch("maintainerd.review.publisher.pull_reviews")
    @patch("maintainerd.review.publisher.pull_request")
    def test_existing_changes_requested_blocks_redundant_peer_review(
        self, pull_request, pull_reviews, review_one
    ):
        pull_request.return_value = {
            "state": "open", "draft": False, "head": {"sha": "abc123"}
        }
        pull_reviews.return_value = [{
            "state": "CHANGES_REQUESTED",
            "commit_id": "abc123",
            "user": {"login": "owner"},
        }]
        self.assertFalse(
            review.sync_peer_once(
                self.state, "mira", self.repository, self.active, 1
            )
        )
        review_one.assert_not_called()

    @patch("maintainerd.review.review_one")
    @patch("maintainerd.review.publisher.pull_reviews", return_value=[])
    @patch("maintainerd.review.publisher.pull_request")
    def test_same_reviewer_only_reviews_each_head_once(
        self, pull_request, pull_reviews, review_one
    ):
        pull_request.return_value = {
            "state": "open", "draft": False, "head": {"sha": "abc123"}
        }
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO review_turns("
                "id,reviewer,repository,pr_number,head_sha,author_maintainer,status,started_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                ("old", "mira", "owner/repo", 31, "abc123", "noah", "completed", "2026-01-02"),
            )
        self.assertFalse(
            review.sync_peer_once(
                self.state, "mira", self.repository, self.active, 1
            )
        )
        review_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
