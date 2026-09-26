"""Recover only demonstrably unposted review attempts, retaining their audit records."""
from __future__ import annotations

import json

from .hydra_state import immediate
from .state import Busy, utcnow


def recover_unposted_reviews(state, actor: str) -> int:
    """The legacy reviewer dedupes every row by head, including failed model calls.

    Archive and release that reservation only when admission was denied or Codex
    itself reported quota/rate limiting before it could return a publishable result.
    A successful model followed by an ambiguous GitHub failure is NOT retried here.
    """
    operation = state.lock('maintainer-' + actor)
    try:
        operation.__enter__()
    except Busy:
        return 0
    try:
        with immediate(state.db):
            state.db.execute('CREATE TABLE IF NOT EXISTS hydra_review_attempts('
                             'id TEXT PRIMARY KEY,record TEXT NOT NULL,archived_at TEXT NOT NULL)')
            rows = state.rows(
                'SELECT r.* FROM review_turns r LEFT JOIN hydra_launches l '
                "ON l.id='review_turns:' || r.id WHERE r.reviewer=? AND r.review_id IS NULL "
                "AND r.status NOT IN ('preparing','running','completed') "
                "AND (l.status IN ('quota','rate_limited') OR (r.invoked=0 AND l.id IS NULL))",
                (actor,))
            for row in rows:
                state.db.execute('INSERT OR IGNORE INTO hydra_review_attempts VALUES (?,?,?)',
                                 (row['id'], json.dumps(row), utcnow()))
                state.db.execute('DELETE FROM review_turns WHERE id=?', (row['id'],))
        if rows:
            print(f'recovery: {len(rows)} unposted review attempt(s) archived; heads eligible again.', flush=True)
        return len(rows)
    finally:
        operation.__exit__(None, None, None)
