"""Private Hydra state, separately versioned inside the existing local SQLite DB."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .hydra_config import Settings, execution_config
from .state import Error, State, utcnow

SCHEMA = '''
CREATE TABLE IF NOT EXISTS hydra_schema(version INTEGER NOT NULL);
INSERT INTO hydra_schema(version) SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM hydra_schema);
CREATE TABLE IF NOT EXISTS hydra_candidates(
 id TEXT PRIMARY KEY, repository TEXT NOT NULL REFERENCES repositories(name),
 source_sha TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL, owner TEXT, claim_token TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 escalations INTEGER NOT NULL DEFAULT 0, validated_sha TEXT, decision TEXT,
 publication_run_id TEXT, issue_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hydra_candidate_queue ON hydra_candidates(repository,status,created_at);
CREATE TABLE IF NOT EXISTS hydra_observations(
 id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES hydra_candidates(id),
 run_id TEXT NOT NULL, actor TEXT NOT NULL, source_sha TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(candidate_id,run_id)
);
CREATE TABLE IF NOT EXISTS hydra_runs(
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, stage TEXT NOT NULL, repository TEXT NOT NULL,
 candidate_id TEXT, status TEXT NOT NULL, source_sha TEXT, result TEXT, error TEXT,
 started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS hydra_launches(
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, stage TEXT NOT NULL, model TEXT,
 effort TEXT, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
 usage TEXT, artifact_path TEXT
);
CREATE INDEX IF NOT EXISTS hydra_launch_day ON hydra_launches(started_at,stage);
CREATE TABLE IF NOT EXISTS hydra_workers(
 actor TEXT PRIMARY KEY, role TEXT NOT NULL, pid INTEGER, state TEXT NOT NULL,
 stage TEXT, model TEXT, effort TEXT, run_id TEXT, detail TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydra_health(
 key TEXT PRIMARY KEY, category TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
'''
LEGACY_TABLES = ('runs', 'thread_turns', 'implementation_steps', 'review_turns')
LEGACY_STAGES = dict(zip(LEGACY_TABLES, ('explore', 'discuss', 'implement', 'review')))


@contextmanager
def immediate(db: sqlite3.Connection):
    """Keep network and model work outside short atomic queue/start transactions."""
    if db.in_transaction:
        raise Error('Hydra cannot start an atomic operation inside another transaction.')
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def connect(home: Path) -> sqlite3.Connection:
    db = sqlite3.connect(home / 'state.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    # No changes to maintainerd's own user_version or its existing rows.
    db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nCOMMIT;')
    versions = [r[0] for r in db.execute('SELECT version FROM hydra_schema')]
    if versions != [1]:
        raise Error('Unsupported Hydra state schema; no downgrade was attempted.')


def bootstrap_launch_history(db: sqlite3.Connection) -> None:
    """Count pre-Hydra launches too, without counting the promoted report twice."""
    with immediate(db):
        available = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in LEGACY_TABLES:
            if table not in available:
                continue
            actor = 'reviewer' if table == 'review_turns' else 'maintainer'
            db.execute(
                f"INSERT OR IGNORE INTO hydra_launches(id,actor,stage,started_at,finished_at,status,usage) "
                f"SELECT ? || id,{actor},?,started_at,finished_at,status,usage FROM {table} "
                "WHERE invoked=1 AND status NOT IN ('preparing','running')",
                (table + ':', LEGACY_STAGES[table]),
            )


def launch_id(artifacts: Path) -> str:
    table = {'runs': 'runs', 'threads': 'thread_turns', 'implementations': 'implementation_steps',
             'reviews': 'review_turns'}.get(artifacts.parent.name)
    return f'{table}:{artifacts.name}' if table else f'hydra:{artifacts.name}'


def used_today(db: sqlite3.Connection, stage: str | None = None) -> int:
    clause, params = '', [utcnow()[:10]]
    if stage is not None:
        clause, params = ' AND stage=?', [*params, stage]
    return db.execute('SELECT count(*) FROM hydra_launches WHERE substr(started_at,1,10)=?' + clause,
                      params).fetchone()[0]


def worker_state(db: sqlite3.Connection, actor: str, role: str, state: str, *, pid: int | None = None,
                 stage: str | None = None, model: str | None = None, effort: str | None = None,
                 run_id: str | None = None, detail: str = '') -> None:
    with db:
        db.execute(
            'INSERT INTO hydra_workers(actor,role,pid,state,stage,model,effort,run_id,detail,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(actor) DO UPDATE SET '
            'role=excluded.role,pid=COALESCE(excluded.pid,hydra_workers.pid),state=excluded.state,'
            'stage=excluded.stage,model=excluded.model,effort=excluded.effort,run_id=excluded.run_id,'
            'detail=excluded.detail,updated_at=excluded.updated_at',
            (actor, role, pid, state, stage, model, effort, run_id, detail[:2000], utcnow()),
        )


def provider_pause(db: sqlite3.Connection, category: str, detail: str) -> None:
    with db:
        db.execute("INSERT INTO hydra_health(key,category,detail,created_at) VALUES ('provider',?,?,?) "
                   'ON CONFLICT(key) DO NOTHING', (category, detail, utcnow()))


def health(db: sqlite3.Connection) -> dict | None:
    row = db.execute("SELECT * FROM hydra_health WHERE key='provider'").fetchone()
    return dict(row) if row else None


class HydraState(State):
    def __init__(self, home: Path, settings: Settings, actor: str = 'operator', role: str = 'core'):
        super().__init__(home)
        ensure_schema(self.db)
        bootstrap_launch_history(self.db)
        self.config = execution_config(self.config, self.home, actor, role, settings)
        for stage in ('scout', 'validate', 'escalate'):
            (self.home / 'hydra' / stage).mkdir(parents=True, exist_ok=True, mode=0o700)

    def remaining(self) -> int | None:
        if self.config.max_runs_per_day == 0:
            return None
        return max(0, self.config.max_runs_per_day - used_today(self.db))

    def can_run(self) -> bool:
        left = self.remaining()
        return not health(self.db) and (left is None or left > 0)

    def coverage(self, repository: str, sha: str) -> list[dict]:
        rows = self.rows(
            "SELECT actor,result FROM hydra_runs WHERE repository=? AND source_sha=? "
            "AND status='completed' ORDER BY started_at DESC LIMIT 100", (repository, sha))
        counts: dict[str, int] = {}
        for row in rows:
            try:
                result = json.loads(row['result'] or '{}')
            except (ValueError, TypeError):
                continue
            for path in set(result.get('inspected_paths') or []):
                counts[path] = counts.get(path, 0) + 1
        return [{'path': p, 'inspections': n}
                for p, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:40]]
