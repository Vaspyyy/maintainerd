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
    "title": "Proposal title",
    "problem": "A concrete problem.",
    "evidence": ["src/example.py:10 demonstrates it."],
    "proposal": "Consider the smallest compatible fix.",
    "tradeoffs": "This tightens behavior.",
    "questions": ["Should this reject or warn?"],
}
RESULT = {
    "outcome": "propose",
    "summary": "Found one useful proposal.",
    "findings": [FINDING],
    "inspected_paths": ["src/example.py"],
    "limitations": ["Tests were not run."],
    "memory_notes": ["Observed the behavior at src/example.py:10."],
}


class PublicationTests(unittest.TestCase):
    def setUp(self):
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

    def test_schema_migrated_for_publications(self):
        version = self.state.db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 2)
        self.assertEqual(
            self.state.db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='proposal_publications'"
            ).fetchone()[0],
            1,
        )

    def test_one_finding_maximum(self):
        with self.assertRaises(Error):
            report.parse(json.dumps({**RESULT, "findings": [FINDING, FINDING]}))

    def test_issue_body_marks_run_and_waits_for_discussion(self):
        body = publisher.issue_body("mira", "run1", "abc123", FINDING)
        self.assertIn("<!-- maintainerd run=run1 finding=0 -->", body)
        self.assertIn("No implementation has been started", body)
        self.assertIn("mira", body)

    @patch("maintainerd.publisher._installation_access")
    @patch("maintainerd.publisher._api")
    def test_publish_creates_and_records_exactly_one_issue(self, api, access):
        access.return_value = {"token": "ephemeral", "permissions": {"issues": "write"}}
        api.side_effect = [
            [],
            {"number": 7, "html_url": "https://github.com/owner/repo/issues/7"},
        ]
        publication = publisher.publish_run(self.state, "run1")
        self.assertEqual(publication["issue_number"], 7)
        self.assertEqual(api.call_count, 2)
        method, path, token, payload = api.call_args_list[1].args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/repos/owner/repo/issues")
        self.assertEqual(token, "ephemeral")
        self.assertEqual(payload["title"], FINDING["title"])
        self.assertIn("maintainerd run=run1", payload["body"])
        report_text = (self.home / "runs/run1/report.md").read_text()
        self.assertIn("https://github.com/owner/repo/issues/7", report_text)

        api.reset_mock()
        again = publisher.publish_run(self.state, "run1")
        self.assertEqual(again["issue_number"], 7)
        api.assert_not_called()

    @patch("maintainerd.publisher._installation_access")
    @patch("maintainerd.publisher._api")
    def test_existing_marker_recovers_without_duplicate_post(self, api, access):
        access.return_value = {"token": "ephemeral", "permissions": {"issues": "write"}}
        api.return_value = [{
            "number": 9,
            "title": FINDING["title"],
            "state": "open",
            "body": "<!-- maintainerd run=run1 finding=0 -->",
            "html_url": "https://github.com/owner/repo/issues/9",
        }]
        publication = publisher.publish_run(self.state, "run1")
        self.assertEqual(publication["issue_number"], 9)
        self.assertEqual(api.call_count, 1)

    @patch("maintainerd.publisher._installation_access")
    @patch("maintainerd.publisher._api")
    def test_matching_open_title_blocks_duplicate(self, api, access):
        access.return_value = {"token": "ephemeral", "permissions": {"issues": "write"}}
        api.return_value = [{
            "number": 2,
            "title": FINDING["title"],
            "state": "open",
            "body": "human issue",
            "html_url": "https://github.com/owner/repo/issues/2",
        }]
        with self.assertRaisesRegex(Error, "already has the title"):
            publisher.publish_run(self.state, "run1")
        self.assertEqual(api.call_count, 1)

    def test_config_requires_app_pair_when_auto_publish_enabled(self):
        path = Path(self.temp.name) / "config.toml"
        path.write_text("publish_proposals=true\n")
        with self.assertRaisesRegex(Error, "requires a configured GitHub App"):
            Config.load(path)


if __name__ == "__main__":
    unittest.main()
