from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from maintainerd import publisher, report
from maintainerd.state import Config, Error, State


FINDING = {
    "title": "Reject duplicate focus IDs before inherited loading",
    "problem": "Inherited focus trees can contain repeated focus IDs and later lose content.",
    "evidence": [
        "src/hoi4/mod.py:945 installs trees without checking nested focus IDs.",
        "src/hoi4/focus.py:411 indexes focuses by ID.",
    ],
    "proposal": "Reject nested duplicate focus IDs before installing inherited trees.",
    "tradeoffs": "Malformed inherited content becomes a load error.",
    "questions": ["Should rejection be atomic?"],
}
RESULT = {
    "outcome": "propose",
    "summary": "Found one useful proposal.",
    "findings": [FINDING],
    "inspected_paths": ["src/hoi4/mod.py", "src/hoi4/focus.py"],
    "limitations": ["Tests were not run."],
    "memory_notes": ["Inherited focus loading lacks nested duplicate checks."],
}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        publisher._SESSION_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"
        self.state = State(self.home)
        self.state.config = replace(
            self.state.config,
            github_app_id=123,
            github_private_key_path="/tmp/fake-key.pem",
        )
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO repositories(name,source,branch,github) VALUES (?,?,?,?)",
                ("sdk", "unused", "main", "owner/repo"),
            )
            self.state.db.execute(
                "INSERT INTO maintainers(name,repository,mission) VALUES (?,?,?)",
                ("mira", "sdk", "Improve it"),
            )
            self.state.db.execute(
                "INSERT INTO runs(id,maintainer,reason,status,started_at,finished_at,commit_sha,result) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("run1", "mira", "exploration", "completed", "2026-01-01T00:00:00+00:00",
                 "2026-01-01T00:01:00+00:00", "abc123", json.dumps(RESULT)),
            )
        artifacts = self.home / "runs" / "run1"
        artifacts.mkdir()
        (artifacts / "context.json").write_text(json.dumps({
            "controller_limitations": ["Model inspection was read-only."],
            "github": {"limitations": []},
        }))

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()
        publisher._SESSION_CACHE.clear()

    def active(self):
        return publisher.Session("owner/repo", "ephemeral", "mira-maintains[bot]")

    def test_schema_migrated_for_coordination(self):
        version = self.state.db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 3)
        for table in ("proposal_routes", "thread_events", "thread_turns"):
            self.assertEqual(
                self.state.db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0],
                1,
            )

    def test_one_finding_maximum(self):
        with self.assertRaises(Error):
            report.parse(json.dumps({**RESULT, "findings": [FINDING, FINDING]}))

    def test_issue_and_overlap_bodies_are_traceable(self):
        body = publisher.issue_body("mira", "run1", "abc123", FINDING)
        overlap = publisher.overlap_comment("mira", "run1", "abc123", FINDING)
        self.assertIn("<!-- maintainerd run=run1 finding=0 -->", body)
        self.assertIn("No implementation has been started", body)
        self.assertIn("overlap-run=run1", overlap)
        self.assertIn("Rather than open a duplicate issue", overlap)

    def test_similarity_detects_same_problem_but_not_unrelated(self):
        same = {
            "title": "Reject duplicate focus IDs before installing an inherited tree",
            "body": "load_inherited_content in src/hoi4/mod.py does not reject duplicate focus IDs.",
        }
        other = {
            "title": "Document localization CSV encoding",
            "body": "Explain UTF-8 BOM handling in docs/localization.md.",
        }
        self.assertGreaterEqual(publisher.similarity(FINDING, same), 0.78)
        self.assertLess(publisher.similarity(FINDING, other), 0.78)

    @patch("maintainerd.publisher.session")
    @patch("maintainerd.publisher._api")
    def test_publish_creates_and_records_exactly_one_issue(self, api, session):
        session.return_value = self.active()
        api.side_effect = [
            [],
            {"number": 7, "html_url": "https://github.com/owner/repo/issues/7"},
        ]
        publication = publisher.publish_run(self.state, "run1")
        self.assertEqual(publication["issue_number"], 7)
        self.assertEqual(publication["mode"], "created")
        self.assertEqual(api.call_count, 2)
        method, path, token, payload = api.call_args_list[1].args
        self.assertEqual((method, path, token), ("POST", "/repos/owner/repo/issues", "ephemeral"))
        self.assertEqual(payload["title"], FINDING["title"])
        self.assertIn("maintainerd run=run1", payload["body"])

        api.reset_mock()
        again = publisher.publish_run(self.state, "run1")
        self.assertEqual(again["issue_number"], 7)
        api.assert_not_called()

    @patch("maintainerd.publisher.session")
    @patch("maintainerd.publisher._api")
    def test_existing_marker_recovers_without_duplicate_post(self, api, session):
        session.return_value = self.active()
        api.return_value = [{
            "number": 9,
            "title": FINDING["title"],
            "state": "open",
            "body": "<!-- maintainerd run=run1 finding=0 -->",
            "html_url": "https://github.com/owner/repo/issues/9",
        }]
        publication = publisher.publish_run(self.state, "run1")
        self.assertEqual(publication["issue_number"], 9)
        self.assertEqual(publication["mode"], "created")
        self.assertEqual(api.call_count, 1)

    @patch("maintainerd.publisher.session")
    @patch("maintainerd.publisher._api")
    def test_overlap_joins_existing_human_thread_instead_of_opening_duplicate(self, api, session):
        session.return_value = self.active()
        existing = {
            "number": 2,
            "title": "Reject duplicate focus IDs before installing an inherited tree",
            "state": "open",
            "body": "Human report about inherited duplicate focuses in src/hoi4/mod.py.",
            "html_url": "https://github.com/owner/repo/issues/2",
        }
        api.side_effect = [
            [existing],
            {"id": 88, "html_url": "https://github.com/owner/repo/issues/2#issuecomment-88"},
        ]
        publication = publisher.publish_run(self.state, "run1")
        self.assertEqual(publication["issue_number"], 2)
        self.assertEqual(publication["mode"], "joined")
        self.assertEqual(publication["comment_id"], 88)
        self.assertEqual(api.call_args_list[1].args[1], "/repos/owner/repo/issues/2/comments")

    @patch("maintainerd.publisher.session")
    @patch("maintainerd.publisher._api")
    def test_same_maintainer_does_not_comment_duplicate_discovery(self, api, session):
        session.return_value = self.active()
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO runs(id,maintainer,reason,status,started_at,finished_at,commit_sha,result) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("old", "mira", "exploration", "completed", "2025-01-01", "2025-01-01", "old", json.dumps(RESULT)),
            )
            self.state.db.execute(
                "INSERT INTO proposal_routes(run_id,finding_index,repository,issue_number,issue_url,title,mode,published_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("old", 0, "owner/repo", 2, "https://github.com/owner/repo/issues/2", FINDING["title"], "created", "2025-01-01"),
            )
        api.return_value = [{
            "number": 2,
            "title": FINDING["title"],
            "state": "open",
            "body": "Same problem.",
            "html_url": "https://github.com/owner/repo/issues/2",
        }]
        with self.assertRaisesRegex(Error, "already has an overlapping open thread"):
            publisher.publish_run(self.state, "run1")
        self.assertEqual(api.call_count, 1)

    @patch("maintainerd.publisher.session")
    @patch("maintainerd.publisher._api")
    def test_closed_overlap_requires_history_review(self, api, session):
        session.return_value = self.active()
        api.return_value = [{
            "number": 3,
            "title": FINDING["title"],
            "state": "closed",
            "body": "Same problem.",
            "html_url": "https://github.com/owner/repo/issues/3",
        }]
        with self.assertRaisesRegex(Error, "overlaps closed GitHub item"):
            publisher.publish_run(self.state, "run1")

    def test_config_requires_app_pair_when_auto_publish_enabled(self):
        path = Path(self.temp.name) / "config.toml"
        path.write_text("publish_proposals=true\n")
        with self.assertRaisesRegex(Error, "requires a configured GitHub App"):
            Config.load(path)


if __name__ == "__main__":
    unittest.main()
