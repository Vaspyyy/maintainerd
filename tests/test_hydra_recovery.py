from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace

from test_hydra import Fixture, CONFIRM
from maintainerd.hydra_execution import admission, Deferred
from maintainerd.hydra_recovery import recover_unposted_reviews
from maintainerd.hydra_state import bootstrap_launch_history, used_today
from maintainerd.hydra_tasks import _make_report


class RecoveryTests(Fixture):
    def review_attempt(self, *, actor='mira', status='quota', ledger='quota', review_id=None, invoked=1):
        with self.state.db:
            # The table exists in the real v0.7 schema. IF NOT EXISTS also permits a
            # small local bootstrap fixture without replacing the real table in CI.
            self.state.db.execute('CREATE TABLE IF NOT EXISTS review_turns('
                                  'id TEXT PRIMARY KEY,reviewer TEXT,repository TEXT,pr_number INTEGER,'
                                  'head_sha TEXT,author_maintainer TEXT,status TEXT,started_at TEXT,'
                                  'finished_at TEXT,result TEXT,error TEXT,usage TEXT,invoked INTEGER,'
                                  'review_id INTEGER,review_url TEXT)')
            self.state.db.execute('INSERT INTO review_turns(id,reviewer,repository,pr_number,head_sha,'
                                  'author_maintainer,status,started_at,invoked,review_id) '
                                  "VALUES ('review-a',?,'owner/repo',2,'abc','noah',?,datetime('now'),?,?)",
                                  (actor, status, invoked, review_id))
            if ledger:
                self.state.db.execute('INSERT INTO hydra_launches(id,actor,stage,started_at,status) '
                                      "VALUES ('review_turns:review-a',?,'review',datetime('now'),?)", (actor, ledger))

    def test_quota_review_is_retryable_without_losing_audit_or_budget(self):
        self.review_attempt()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recover_unposted_reviews(self.state, 'mira'), 1)
        self.assertEqual(self.state.rows('SELECT * FROM review_turns'), [])
        row = self.state.rows('SELECT * FROM hydra_review_attempts')[0]
        self.assertEqual(json.loads(row['record'])['head_sha'], 'abc')
        self.assertEqual(used_today(self.state.db), 1)

    def test_posted_review_is_never_replayed(self):
        self.review_attempt(review_id=44)
        self.assertEqual(recover_unposted_reviews(self.state, 'mira'), 0)

    def test_ambiguous_publication_failure_is_not_automatically_retried(self):
        self.review_attempt(status='failed', ledger='completed')
        self.assertEqual(recover_unposted_reviews(self.state, 'mira'), 0)

    def test_review_recovery_is_scoped_to_its_actor(self):
        self.review_attempt(actor='noah')
        self.assertEqual(recover_unposted_reviews(self.state, 'mira'), 0)

    def test_denied_admission_is_not_later_counted_as_legacy_launch(self):
        self.state.config = replace(self.state.config, max_runs_per_day=1)
        first = self.home / 'hydra/validate/first'; first.mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            with admission(self.state.config, first):
                pass
        with self.state.db:
            self.state.db.execute('INSERT INTO runs(id,maintainer,reason,status,started_at,invoked) '
                                  "VALUES ('denied','mira','exploration','running',datetime('now'),1)")
        denied = self.home / 'runs/denied'; denied.mkdir()
        with self.assertRaises(Deferred):
            with admission(self.state.config, denied):
                self.fail('No capacity left')
        self.assertEqual(self.state.one('runs', 'denied')['invoked'], 0)
        with self.state.db:
            self.state.db.execute("UPDATE runs SET status='failed' WHERE id='denied'")
        bootstrap_launch_history(self.state.db)
        self.assertEqual(used_today(self.state.db), 1)

    def test_revalidation_report_uses_current_core_identity(self):
        candidate_id = self.candidate()
        candidate = self.state.rows('SELECT * FROM hydra_candidates WHERE id=?', (candidate_id,))[0]
        report_id = _make_report(self.state, 'mira', candidate, 'first', self.sha, CONFIRM)
        candidate['publication_run_id'] = report_id
        second = _make_report(self.state, 'noah', candidate, 'second', 'new-sha', CONFIRM)
        self.assertEqual(report_id, second)
        self.assertEqual(self.state.one('runs', report_id)['maintainer'], 'noah')
        self.assertEqual(self.state.one('runs', report_id)['commit_sha'], 'new-sha')
