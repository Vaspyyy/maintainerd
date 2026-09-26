"""Private candidate clustering and fenced, single-host validation claims.

This module deliberately has no GitHub client, publication action or model client.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import PurePosixPath

from . import report
from .hydra_state import HydraState, immediate, used_today
from .state import Busy, Error, utcnow

TEXT = {'type': 'string', 'maxLength': 6000}
STRINGS = {'type': 'array', 'items': TEXT, 'maxItems': 20}
CANDIDATE = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'title': {'type': 'string', 'minLength': 1, 'maxLength': 200},
        'problem': TEXT, 'root_cause': TEXT,
        'paths': {**STRINGS, 'minItems': 1}, 'symbols': STRINGS,
        'evidence': {**STRINGS, 'minItems': 1}, 'suggested_reproduction': TEXT,
        'confidence': {'type': 'string', 'enum': ['low', 'medium', 'high']},
    },
    'required': ['title', 'problem', 'root_cause', 'paths', 'symbols', 'evidence',
                 'suggested_reproduction', 'confidence'],
}
SCOUT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'outcome': {'type': 'string', 'enum': ['findings', 'no_action']},
        'summary': TEXT, 'findings': {'type': 'array', 'items': CANDIDATE, 'maxItems': 2},
        'inspected_paths': STRINGS, 'limitations': STRINGS,
    },
    'required': ['outcome', 'summary', 'findings', 'inspected_paths', 'limitations'],
}
VALIDATION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'action': {'type': 'string', 'enum': ['confirm', 'reject', 'duplicate', 'escalate', 'needs_input']},
        'summary': TEXT, 'reason': TEXT, 'duplicate_of': TEXT,
        'findings': {'type': 'array', 'items': report.FINDING, 'maxItems': 1},
        'inspected_paths': STRINGS, 'limitations': STRINGS,
    },
    'required': ['action', 'summary', 'reason', 'duplicate_of', 'findings', 'inspected_paths', 'limitations'],
}
ACTIVE = ('queued', 'validating', 'escalated', 'validating_escalated', 'accepted')


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or len(value) > 512 or '\\' in value or any(ord(c) < 32 for c in value)
            or path.is_absolute() or '..' in path.parts or '.git' in path.parts):
        raise Error('Candidate paths must be relative repository paths, without traversal or .git.')
    return str(path)


def parse_scout(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Error('Scout did not return valid JSON.') from exc
    report.check(value, SCOUT_SCHEMA, 'scout')
    if (value['outcome'] == 'findings') != bool(value['findings']):
        raise Error('Scout outcome and findings disagree.')
    for finding in value['findings']:
        if not finding['root_cause'].strip() or not finding['problem'].strip():
            raise Error('A private finding needs a nonempty root cause and problem.')
        finding['paths'] = sorted({safe_path(p) for p in finding['paths']})
        finding['symbols'] = sorted({s.strip() for s in finding['symbols'] if s.strip()})
    for path in value['inspected_paths']:
        safe_path(path)
    return value


def parse_validation(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Error('Validation did not return valid JSON.') from exc
    report.check(value, VALIDATION_SCHEMA, 'validation')
    if (value['action'] == 'confirm') != (len(value['findings']) == 1):
        raise Error('Only confirmed findings can contain a public proposal.')
    if not value['reason'].strip():
        raise Error('Validation needs an evidence-based reason.')
    if value['action'] == 'duplicate' and not value['duplicate_of'].strip():
        raise Error('Duplicate validation must identify the existing work.')
    if value['action'] != 'duplicate' and value['duplicate_of'].strip():
        raise Error('Only duplicate decisions may name duplicate_of.')
    for path in value['inspected_paths']:
        safe_path(path)
    return value


def words(text: str) -> set[str]:
    return set(re.findall(r'[a-z0-9_]+', text.casefold()))


def fingerprint(candidate: dict) -> str:
    # Root cause is essential: sharing a large file and generic symbols is not enough.
    stable = {'cause': sorted(words(candidate['root_cause'])), 'paths': sorted(candidate['paths']),
              'symbols': sorted(s.casefold() for s in candidate['symbols'])}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def same_problem(a: dict, b: dict) -> bool:
    if not set(a['paths']) & set(b['paths']):
        return False
    left, right = words(a['root_cause']), words(b['root_cause'])
    similarity = len(left & right) / max(1, len(left | right))
    symbols = set(a['symbols']) & set(b['symbols'])
    return (left == right and bool(left)) or (similarity >= .80 and bool(symbols))


def pending_count(state: HydraState, repository: str) -> int:
    marks = ','.join('?' for _ in ACTIVE)
    return state.db.execute(f'SELECT count(*) FROM hydra_candidates WHERE repository=? '
                            f'AND status IN ({marks})', (repository, *ACTIVE)).fetchone()[0]


def submit(state: HydraState, repository: str, actor: str, run_id: str, sha: str,
           candidate: dict) -> tuple[str | None, bool]:
    """Return (cluster ID, new cluster); queue capacity is enforced atomically."""
    with immediate(state.db):
        rows = state.rows(
            "SELECT * FROM hydra_candidates WHERE repository=? AND "
            "(status IN ('queued','validating','escalated','validating_escalated','accepted') "
            "OR source_sha=?) ORDER BY created_at DESC LIMIT 1000", (repository, sha))
        existing = next((r for r in rows if same_problem(candidate, json.loads(r['payload']))), None)
        created = existing is None
        if created:
            if pending_count(state, repository) >= state.config.hydra_settings.queue_limit:
                return None, False
            candidate_id = uuid.uuid4().hex[:16]
            state.db.execute(
                'INSERT INTO hydra_candidates(id,repository,source_sha,fingerprint,payload,status,created_at,updated_at) '
                "VALUES (?,?,?,?,?,'queued',?,?)",
                (candidate_id, repository, sha, fingerprint(candidate), json.dumps(candidate), utcnow(), utcnow()))
        else:
            candidate_id = existing['id']
        # Bounded corroboration per candidate; the full scout result remains in its run artifact.
        count = state.db.execute('SELECT count(*) FROM hydra_observations WHERE candidate_id=?',
                                 (candidate_id,)).fetchone()[0]
        if count < 64:
            state.db.execute(
                'INSERT OR IGNORE INTO hydra_observations(candidate_id,run_id,actor,source_sha,payload,created_at) '
                'VALUES (?,?,?,?,?,?)', (candidate_id, run_id, actor, sha, json.dumps(candidate), utcnow()))
        return candidate_id, created


@contextmanager
def claim_next(state: HydraState, repository: str, owner: str):
    """A file lock survives orphaned Codex children; the DB token fences stale results.

    No wall-clock lease can make a second validator race a still-live first one.
    Recover a crashed 'validating' row only after obtaining its actual OS lock.
    """
    rows = state.rows(
        "SELECT * FROM hydra_candidates WHERE repository=? AND status IN "
        "('queued','escalated','validating','validating_escalated') ORDER BY created_at,id LIMIT 200",
        (repository,))
    for candidate in rows:
        with_context = state.lock('hydra-candidate-' + candidate['id'])
        try:
            fd = with_context.__enter__()
        except Busy:
            continue
        try:
            with immediate(state.db):
                current = state.db.execute('SELECT * FROM hydra_candidates WHERE id=?',
                                           (candidate['id'],)).fetchone()
                if current['status'] not in ('queued', 'escalated', 'validating', 'validating_escalated'):
                    continue
                if current['attempts'] >= state.config.hydra_settings.max_validation_attempts:
                    state.db.execute("UPDATE hydra_candidates SET status='needs_input',decision=?,updated_at=? WHERE id=?",
                                     ('Validation attempt limit reached; manual review required.', utcnow(), candidate['id']))
                    continue
                stage = 'escalate' if current['status'] in ('escalated', 'validating_escalated') else 'validate'
                if stage == 'escalate' and used_today(state.db, 'escalate') >= state.config.hydra_settings.max_escalations_per_day:
                    continue
                token = uuid.uuid4().hex
                state.db.execute('UPDATE hydra_candidates SET status=?,owner=?,claim_token=?,updated_at=? WHERE id=?',
                                 ('validating_escalated' if stage == 'escalate' else 'validating',
                                  owner, token, utcnow(), candidate['id']))
                candidate = dict(current)
                candidate.update(owner=owner, claim_token=token, stage=stage)
            try:
                yield candidate, fd
            finally:
                # Unfinished claims become retryable. The exception classification decides whether
                # the worker itself is paused; this never grants publication authority.
                with state.db:
                    state.db.execute(
                        'UPDATE hydra_candidates SET status=?,claim_token=NULL,updated_at=? '
                        "WHERE id=? AND claim_token=? AND status IN ('validating','validating_escalated')",
                        ('escalated' if stage == 'escalate' else 'queued', utcnow(), candidate['id'], token))
            return
        finally:
            with_context.__exit__(None, None, None)
    yield None, None


def update_claim(state: HydraState, candidate: dict, status: str, **values) -> None:
    if set(values) - {'decision', 'validated_sha', 'publication_run_id', 'issue_url', 'escalations'}:
        raise Error('Unsupported private candidate update.')
    assignments = ['status=?', 'updated_at=?'] + [f'{key}=?' for key in values]
    with state.db:
        changed = state.db.execute(
            f"UPDATE hydra_candidates SET {','.join(assignments)} WHERE id=? AND claim_token=?",
            (status, utcnow(), *values.values(), candidate['id'], candidate['claim_token'])).rowcount
    if changed != 1:
        raise Error('Candidate ownership changed; stale validation cannot publish.')
