from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from maintainerd import codex, repo
from maintainerd.hydra_config import Settings, resolve, template
from maintainerd.hydra_execution import Deferred, ProviderPaused, admission, failure_category
from maintainerd.hydra_pool import (claim_next, parse_scout, parse_validation, pending_count,
                                    same_problem, submit, update_claim)
from maintainerd.hydra_state import HydraState, health, provider_pause, used_today
from maintainerd.hydra_tasks import scout_once, validate_once, publish_accepted
from maintainerd.state import Config, Error

CANDIDATE = {
    'title': 'Preserve queued values', 'problem': 'A later update discards earlier values.',
    'root_cause': 'The setter replaces the pending dictionary instead of merging it.',
    'paths': ['src/example.py'], 'symbols': ['update_values'],
    'evidence': ['src/example.py stores only the replacement dictionary.'],
    'suggested_reproduction': 'Call update_values twice with different keys; check both survive.',
    'confidence': 'medium',
}
SCOUT = {'outcome': 'findings', 'summary': 'One candidate for independent review.',
         'findings': [CANDIDATE], 'inspected_paths': ['src/example.py'], 'limitations': ['Static inspection only.']}
FINDING = {
    'title': CANDIDATE['title'], 'problem': CANDIDATE['problem'], 'evidence': CANDIDATE['evidence'],
    'proposal': 'Merge queued values and add a regression.', 'tradeoffs': 'Preserve intended ordering.',
    'questions': ['Should repeated writes keep the latest value for each key?'],
}
CONFIRM = {'action': 'confirm', 'summary': 'Current source supports the finding.',
           'reason': 'I inspected the current setter and the existing tests.', 'duplicate_of': '',
           'findings': [FINDING], 'inspected_paths': ['src/example.py'], 'limitations': ['Tests were not executed.']}
REJECT = {**CONFIRM, 'action': 'reject', 'reason': 'The contract explicitly requires replacement.', 'findings': []}


def build_fake(path, result=None, error=None):
    script = '''#!/usr/bin/env python3
import json,sys
from pathlib import Path
RESULT = %s
ERROR = %s
if '--version' in sys.argv:
 print('codex-test 0.157.0'); sys.exit(0)
if '--help' in sys.argv:
 print('--json --sandbox --output-schema --output-last-message --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --strict-config workspace-write'); sys.exit(0)
if sys.argv[1:3] == ['login','status']:
 print('Logged in using ChatGPT'); sys.exit(0)
sys.stdin.read()
if ERROR:
 print(json.dumps({'type':'turn.failed','error':{'message':ERROR}})); sys.exit(1)
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(RESULT))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'cached_input_tokens':4,'output_tokens':3}}))
''' % (repr(result), repr(error))
    path.write_text(script)
    path.chmod(0o700)


def concurrent_probe(home, settings, barrier, outputs, index, mode):
    """Separate processes, separate SQLite connections, simultaneous queue/start transactions."""
    state = HydraState(Path(home), settings, f'scout-{index}', 'scout')
    state.config = replace(state.config, max_runs_per_day=1)
    try:
        barrier.wait(timeout=10)
        if mode == 'submit':
            outputs.put(submit(state, 'sdk', f'scout-{index}', f'parallel-{index}', 'same-sha', CANDIDATE))
        else:
            path = state.home / 'hydra' / 'scout' / f'parallel-{index}'
            path.mkdir(parents=True)
            try:
                with admission(state.config, path):
                    outputs.put('admitted')
            except Deferred:
                outputs.put('deferred')
    finally:
        state.close()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'state'
        self.settings_path = self.root / 'hydra.toml'
        self.settings_path.write_text(template('test-luna', 'test-sol', 'test-astra'))
        self.settings = Settings.load(self.settings_path)
        self.state = HydraState(self.home, self.settings, 'mira', 'core')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.state.close)
        self.source = self.root / 'source'
        self.source.mkdir()
        for args in (['init', '-b', 'main'], ['config', 'user.name', 'Test'],
                     ['config', 'user.email', 'test@example.invalid']):
            subprocess.run(['git', *args], cwd=self.source, check=True, capture_output=True)
        (self.source / 'src').mkdir()
        (self.source / 'src/example.py').write_text('def update_values(values):\n    return dict(values)\n')
        subprocess.run(['git', 'add', '.'], cwd=self.source, check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'fixture'], cwd=self.source, check=True, capture_output=True)
        repo.add(self.state, str(self.source), 'sdk', 'main')
        with self.state.db:
            self.state.db.executemany('INSERT INTO maintainers(name,repository,mission) VALUES (?,?,?)',
                                     [('mira', 'sdk', 'Improve thoughtfully'), ('noah', 'sdk', 'Improve independently')])
        self.fake = self.root / 'codex'
        build_fake(self.fake, SCOUT)
        self.state.config = replace(self.state.config, codex_binary=str(self.fake), max_runs_per_day=0)
        self.sha = repo.git(['rev-parse', 'HEAD'], cwd=self.source).strip()

    def candidate(self, payload=None, sha=None):
        return submit(self.state, 'sdk', 'scout-001', 'run1', sha or self.sha, payload or CANDIDATE)[0]

    def scout_state(self):
        state = HydraState(self.home, self.settings, 'scout-001', 'scout')
        state.config = replace(state.config, codex_binary=str(self.fake), max_runs_per_day=0)
        self.addCleanup(state.close)
        return state


class ProfileTests(Fixture):
    def test_stage_routes_are_explicit_and_effort_is_passed(self):
        for folder, stage, model in [('runs', 'explore', 'test-luna'), ('threads', 'discuss', 'test-luna'),
                                      ('reviews', 'review', 'test-sol'), ('implementations', 'implement', 'test-sol'),
                                      ('validate', 'validate', 'test-sol'), ('escalate', 'escalate', 'test-astra')]:
            config = resolve(self.state.config, Path('/tmp') / folder / 'test')
            self.assertEqual(config.model, model)
            self.assertEqual(config.execution_stage, stage)
            args = codex.argv(config, Path('/tmp/control'), Path('/tmp/result'))
            self.assertIn('model_reasoning_effort="medium"', args)
            self.assertEqual(args[args.index('--model') + 1], model)
            self.assertNotIn('--yolo', args)

    def test_legacy_config_has_no_model_routing_side_effect(self):
        config = Config(model='existing-selected-model')
        self.assertIs(resolve(config, Path('/unrecognized/test')), config)

    def test_scout_config_has_no_app_identity(self):
        state = self.scout_state()
        self.assertIsNone(state.config.github_app_id)
        self.assertIsNone(state.config.github_private_key_path)
        self.assertFalse(state.config.publish_proposals)
        self.assertFalse(state.config.include_github)
        with self.assertRaises(Error):
            resolve(state.config, Path('/tmp/reviews/test'))

    def test_unknown_profile_or_config_is_rejected(self):
        for bad in ['[hydra]\nscouts=true\n', '[hydra]\nunknown=2\n', '[profiles.scout]\nmodel="x"\n']:
            self.settings_path.write_text(bad)
            with self.assertRaises(Error):
                Settings.load(self.settings_path)

    def test_profile_cannot_include_credentials_or_enable_tools(self):
        self.settings_path.write_text(template('a', 'b', None) + '\napi_key="secret"\n')
        with self.assertRaises(Error):
            Settings.load(self.settings_path)

    def test_unknown_stage_has_no_expensive_default(self):
        with self.assertRaises(Error):
            resolve(self.state.config, Path('/unknown/result'))


class PoolTests(Fixture):
    def test_private_duplicates_cluster_with_provenance(self):
        a, created = submit(self.state, 'sdk', 'scout-001', 'r1', self.sha, CANDIDATE)
        b, second = submit(self.state, 'sdk', 'scout-002', 'r2', self.sha, {**CANDIDATE, 'title': 'Different wording'})
        self.assertEqual(a, b)
        self.assertTrue(created)
        self.assertFalse(second)
        self.assertEqual(pending_count(self.state, 'sdk'), 1)
        self.assertEqual(len(self.state.rows('SELECT * FROM hydra_observations')), 2)
        self.assertEqual(self.state.rows('SELECT * FROM runs'), [])

    def test_shared_symbols_do_not_merge_different_root_causes(self):
        other = {**CANDIDATE, 'root_cause': 'The parser incorrectly splits quoted unicode escape sequences.'}
        self.assertFalse(same_problem(CANDIDATE, other))
        self.candidate()
        second, created = submit(self.state, 'sdk', 'scout-001', 'r2', self.sha, other)
        self.assertTrue(created)
        self.assertEqual(pending_count(self.state, 'sdk'), 2)

    def test_queue_limit_is_enforced_at_submission(self):
        self.state.config = replace(self.state.config, hydra_settings=replace(self.settings, queue_limit=1))
        self.candidate()
        result = submit(self.state, 'sdk', 'scout-002', 'r2', self.sha,
                        {**CANDIDATE, 'root_cause': 'Wrong character encoding corrupts names.'})
        self.assertEqual(result, (None, False))

    def test_read_only_candidate_paths_reject_escape(self):
        for path in ['/etc/passwd', '../secret', '.git/config', 'src/../../x']:
            with self.assertRaises(Error):
                parse_scout(json.dumps({**SCOUT, 'findings': [{**CANDIDATE, 'paths': [path]}]}))

    def test_claim_is_exclusive_and_failed_turn_recovers(self):
        candidate_id = self.candidate()
        second = HydraState(self.home, self.settings, 'noah')
        self.addCleanup(second.close)
        with claim_next(self.state, 'sdk', 'mira') as (a, fd):
            self.assertEqual(a['id'], candidate_id)
            with claim_next(second, 'sdk', 'noah') as (b, _):
                self.assertIsNone(b)
        with claim_next(second, 'sdk', 'noah') as (b, _):
            self.assertEqual(b['id'], candidate_id)

    def test_stale_token_cannot_accept_a_finding(self):
        candidate_id = self.candidate()
        with claim_next(self.state, 'sdk', 'mira') as (candidate, _):
            with self.state.db:
                self.state.db.execute('UPDATE hydra_candidates SET claim_token=? WHERE id=?', ('new', candidate_id))
            with self.assertRaises(Error):
                update_claim(self.state, candidate, 'accepted')

    def test_rejected_snapshot_dedupes_but_new_commit_can_be_revalidated(self):
        candidate_id = self.candidate()
        with self.state.db:
            self.state.db.execute("UPDATE hydra_candidates SET status='rejected' WHERE id=?", (candidate_id,))
        same, created = submit(self.state, 'sdk', 'scout-002', 'r2', self.sha, CANDIDATE)
        self.assertFalse(created)
        new, created = submit(self.state, 'sdk', 'scout-002', 'r3', 'new-head', CANDIDATE)
        self.assertTrue(created)
        self.assertNotEqual(same, new)

    def test_escalation_limit_does_not_starve_other_candidates(self):
        first = self.candidate()
        with self.state.db:
            self.state.db.execute("UPDATE hydra_candidates SET status='escalated' WHERE id=?", (first,))
        self.state.config = replace(self.state.config, hydra_settings=replace(self.settings, max_escalations_per_day=0))
        second, _ = submit(self.state, 'sdk', 'scout-002', 'r2', self.sha,
                           {**CANDIDATE, 'root_cause': 'A counter underflows when zero is subtracted.'})
        with claim_next(self.state, 'sdk', 'mira') as (chosen, _):
            self.assertEqual(chosen['id'], second)


class ExecutionTests(Fixture):
    def test_error_classifier_ignores_normal_model_and_tool_output(self):
        events, stderr = self.root / 'events', self.root / 'stderr'
        events.write_text(json.dumps({'type': 'item.completed', 'item': {'text': 'usage limit reached'}})+'\n')
        stderr.write_text('')
        self.assertEqual(failure_category(events, stderr), 'failed')
        events.write_text(json.dumps({'type': 'turn.failed', 'error': {'code': 'usage_limit_reached'}})+'\n')
        self.assertEqual(failure_category(events, stderr), 'quota')

    def test_quota_sets_shared_circuit_and_preserves_candidate(self):
        candidate_id = self.candidate()
        build_fake(self.fake, error="You've hit your usage limit")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(codex.RuntimeFailure):
            validate_once(self.state, 'mira', 'sdk')
        self.assertEqual(health(self.state.db)['category'], 'quota')
        self.assertEqual(self.state.rows('SELECT status FROM hydra_candidates WHERE id=?', (candidate_id,))[0]['status'], 'queued')
        self.assertEqual(used_today(self.state.db), 1)
        self.assertFalse(self.state.can_run())

    def test_new_launch_refused_when_provider_is_paused(self):
        provider_pause(self.state.db, 'quota', 'Test pause')
        artifacts = self.root / 'validate' / 'a'; artifacts.mkdir(parents=True)
        with self.assertRaises(ProviderPaused):
            with admission(self.state.config, artifacts):
                self.fail('A paused provider must never admit a model launch.')
        self.assertEqual(used_today(self.state.db), 0)

    def test_start_budget_is_atomic_across_connections(self):
        self.state.config = replace(self.state.config, max_runs_per_day=1)
        a = self.root / 'validate' / 'a'; a.mkdir(parents=True)
        b = self.root / 'validate' / 'b'; b.mkdir(parents=True)
        with admission(self.state.config, a):
            with self.assertRaises(Deferred):
                with admission(self.state.config, b):
                    self.fail('Budget overspend')
        self.assertEqual(used_today(self.state.db), 1)

    def test_unlimited_still_tracks_tokens_and_routes_model(self):
        scout = self.scout_state()
        with contextlib.redirect_stdout(io.StringIO()):
            scout_once(scout, 'sdk', 'scout-001')
        row = scout.rows('SELECT * FROM hydra_launches')[0]
        self.assertEqual(row['model'], 'test-luna')
        self.assertEqual(json.loads(row['usage'])['input_tokens'], 12)
        self.assertIsNone(scout.remaining())
        self.assertEqual(len(scout.rows('SELECT * FROM hydra_candidates')), 1)
        self.assertEqual(scout.rows('SELECT * FROM runs'), [])

    def test_old_rows_are_counted_without_changing_their_contents(self):
        with self.state.db:
            self.state.db.execute('INSERT INTO runs(id,maintainer,reason,status,started_at,finished_at,invoked) '
                                  "VALUES ('old','mira','explore','failed',datetime('now'),datetime('now'),1)")
        from maintainerd.hydra_state import bootstrap_launch_history
        bootstrap_launch_history(self.state.db); bootstrap_launch_history(self.state.db)
        self.assertEqual(used_today(self.state.db), 1)
        self.assertEqual(self.state.one('runs', 'old')['status'], 'failed')


class ValidationTests(Fixture):
    def test_scout_cannot_enter_core_validation(self):
        with self.assertRaises(Error):
            validate_once(self.scout_state(), 'mira', 'sdk')

    def test_only_core_confirmation_produces_a_publishable_report(self):
        candidate_id = self.candidate()
        build_fake(self.fake, CONFIRM)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(validate_once(self.state, 'mira', 'sdk'))
        item = self.state.rows('SELECT * FROM hydra_candidates WHERE id=?', (candidate_id,))[0]
        self.assertEqual(item['status'], 'accepted')
        self.assertEqual(item['validated_sha'], self.sha)
        run = self.state.one('runs', item['publication_run_id'])
        self.assertEqual(run['invoked'], 0)
        self.assertEqual(run['maintainer'], 'mira')
        self.assertEqual(used_today(self.state.db), 1)
        self.assertIn('no reproduction was executed', json.loads(run['result'])['limitations'][-1])

    def test_rejection_is_private_and_does_not_open_a_report(self):
        self.candidate(); build_fake(self.fake, REJECT)
        with contextlib.redirect_stdout(io.StringIO()):
            validate_once(self.state, 'mira', 'sdk')
        self.assertEqual(self.state.rows('SELECT status FROM hydra_candidates')[0]['status'], 'rejected')
        self.assertEqual(self.state.rows('SELECT * FROM runs'), [])

    def test_escalation_is_bounded_to_one_stronger_turn(self):
        self.candidate()
        build_fake(self.fake, {**REJECT, 'action': 'escalate', 'reason': 'Needs a deeper root-cause analysis.'})
        with contextlib.redirect_stdout(io.StringIO()):
            validate_once(self.state, 'mira', 'sdk')
            self.assertEqual(self.state.rows('SELECT status FROM hydra_candidates')[0]['status'], 'escalated')
            validate_once(self.state, 'noah', 'sdk')
        row = self.state.rows('SELECT * FROM hydra_candidates')[0]
        self.assertEqual(row['status'], 'needs_input')
        self.assertEqual(row['escalations'], 1)
        self.assertEqual([r['model'] for r in self.state.rows('SELECT model FROM hydra_launches ORDER BY rowid')], ['test-sol', 'test-astra'])
        self.assertFalse(validate_once(self.state, 'mira', 'sdk'))

    def test_invalid_confirmation_is_not_promoted(self):
        self.candidate(); build_fake(self.fake, {**CONFIRM, 'findings': []})
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Error):
            validate_once(self.state, 'mira', 'sdk')
        self.assertEqual(self.state.rows('SELECT * FROM runs'), [])

    def test_validation_cannot_smuggle_findings_through_reject(self):
        with self.assertRaises(Error):
            parse_validation(json.dumps({**CONFIRM, 'action': 'reject'}))

    def test_publication_uses_core_broker_once(self):
        self.candidate(); build_fake(self.fake, CONFIRM)
        with contextlib.redirect_stdout(io.StringIO()):
            validate_once(self.state, 'mira', 'sdk')
        row = self.state.rows('SELECT * FROM hydra_candidates')[0]
        self.state.config = replace(self.state.config, publish_proposals=True)
        with patch('maintainerd.publisher.publish_run', return_value={'issue_url':'https://github.com/owner/repo/issues/7'}) as broker, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(publish_accepted(self.state, 'mira', 'sdk'))
            self.assertFalse(publish_accepted(self.state, 'mira', 'sdk'))
        broker.assert_called_once_with(self.state, row['publication_run_id'])
        self.assertEqual(self.state.rows('SELECT status FROM hydra_candidates')[0]['status'], 'published')

    def test_accepted_findings_are_revalidated_after_source_changes(self):
        self.candidate(); build_fake(self.fake, CONFIRM)
        with contextlib.redirect_stdout(io.StringIO()):
            validate_once(self.state, 'mira', 'sdk')
        old_report = self.state.rows('SELECT publication_run_id FROM hydra_candidates')[0]['publication_run_id']
        (self.source / 'src/example.py').write_text('def update_values(values):\n    return dict(values) # changed\n')
        repo.git(['add','.'], cwd=self.source); repo.git(['commit','-m','change'], cwd=self.source)
        self.state.config = replace(self.state.config, publish_proposals=True)
        with patch('maintainerd.publisher.publish_run') as broker, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(publish_accepted(self.state, 'mira', 'sdk'))
        broker.assert_not_called()
        self.assertEqual(self.state.rows('SELECT status FROM hydra_candidates')[0]['status'], 'queued')
        self.state.config = replace(self.state.config, publish_proposals=False)
        with contextlib.redirect_stdout(io.StringIO()):
            validate_once(self.state, 'mira', 'sdk')
        self.assertEqual(self.state.rows('SELECT publication_run_id FROM hydra_candidates')[0]['publication_run_id'], old_report)
        self.assertEqual(len(self.state.rows('SELECT * FROM runs')), 1)


class ConcurrencyTests(Fixture):
    def run_processes(self, mode):
        context = multiprocessing.get_context('spawn')
        barrier, outputs = context.Barrier(4), context.Queue()
        settings = replace(self.settings, max_parallel=8, reserved_core_slots=1)
        children = [context.Process(target=concurrent_probe, args=(str(self.home), settings, barrier, outputs, i, mode)) for i in range(4)]
        try:
            for child in children:
                child.start()
            values = [outputs.get(timeout=20) for _ in children]
            for child in children:
                child.join(timeout=5)
                self.assertEqual(child.exitcode, 0)
            return values
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            outputs.close()
            outputs.join_thread()

    def test_simultaneous_submissions_make_one_cluster(self):
        values = self.run_processes('submit')
        self.assertEqual(len({v[0] for v in values}), 1)
        self.assertEqual(sum(v[1] for v in values), 1)
        self.assertEqual(len(self.state.rows('SELECT * FROM hydra_observations')), 4)

    def test_simultaneous_launches_respect_shared_finite_cap(self):
        values = self.run_processes('admit')
        self.assertEqual(values.count('admitted'), 1)
        self.assertEqual(used_today(self.state.db), 1)


class RuntimeTests(Fixture):
    def test_mux_flushes_all_lines_and_split_unicode(self):
        from maintainerd.hydra_runtime import LineMux
        mux = LineMux()
        self.assertEqual(mux.feed('mira', b'one\ntwo\n'), ['[mira] one', '[mira] two'])
        encoded = 'hello λ\n'.encode()
        self.assertEqual(mux.feed('noah', encoded[:-2]), [])
        self.assertEqual(mux.feed('noah', encoded[-2:]), ['[noah] hello λ'])
        self.assertEqual(mux.feed('iris', b'partial', final=True), ['[iris] partial'])

    def test_one_worker_exit_does_not_kill_healthy_peer(self):
        from maintainerd.hydra_runtime import supervise
        marker = self.root / 'peer-completed'
        def argv(home, repository, actor, role, index, total):
            if actor == 'mira':
                return [sys.executable, '-c', "print('failed'); raise SystemExit(1)"]
            return [sys.executable, '-c', f"import time;from pathlib import Path;time.sleep(.3);Path({str(marker)!r}).write_text('finished');print('done')"]
        with patch('maintainerd.cli._serve_identity_check', return_value=None), \
             patch('maintainerd.hydra_runtime.worker_argv', side_effect=argv), \
             contextlib.redirect_stdout(io.StringIO()):
            code = supervise(self.state, 'sdk', ['mira', 'noah'], 0)
        self.assertEqual(code, 1)
        self.assertEqual(marker.read_text(), 'finished')

    def test_quota_worker_pauses_without_repeated_model_calls(self):
        from maintainerd.hydra_runtime import worker_loop
        scout = self.scout_state()
        build_fake(self.fake, error='usage_limit_reached')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker_loop(scout, 'sdk', 'scout-001', 'scout', once=True), 75)
            self.assertEqual(worker_loop(scout, 'sdk', 'scout-001', 'scout', once=True), 75)
        self.assertEqual(used_today(scout.db), 1)

    def test_scout_writable_request_never_reserves_a_launch(self):
        scout = self.scout_state()
        path = self.home / 'hydra/scout/illegal'; path.mkdir(parents=True)
        with self.state.lock('test') as fd, self.assertRaises(Error):
            codex.execute(scout.config, path, 'not allowed', fd, sandbox='workspace-write', stage='scout')
        self.assertEqual(used_today(self.state.db), 0)

    def test_cli_init_does_not_overwrite_existing_files(self):
        from maintainerd.hydra_cli import main
        before = (self.home / 'config.toml').read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--home', str(self.home), 'hydra', 'init']), 0)
        content = (self.home / 'hydra.toml').read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--home', str(self.home), 'hydra', 'init']), 1)
        self.assertEqual(before, (self.home / 'config.toml').read_bytes())
        self.assertEqual(content, (self.home / 'hydra.toml').read_bytes())


if __name__ == '__main__':
    unittest.main()
