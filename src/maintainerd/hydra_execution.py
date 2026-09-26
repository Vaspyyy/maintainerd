"""Hydra's model admission boundary: profiles, slots, start budgets and provider pauses."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .hydra_config import ExecutionConfig, resolve
from .hydra_state import connect, health, immediate, launch_id, provider_pause, used_today, worker_state
from .state import Error, utcnow


class Deferred(Error):
    """No model was launched; capacity or a local policy currently prevents it."""


class ProviderPaused(Deferred):
    pass


def failure_category(events_path: Path, stderr_path: Path) -> str:
    """Read actual error events, never ordinary model/tool text, for classification."""
    errors: list[str] = []
    if events_path.exists():
        with events_path.open(encoding='utf-8', errors='replace') as source:
            for line in source:
                if len(line) > 512_000:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('type') == 'turn.failed':
                    errors.append(json.dumps(event.get('error', {}))[:8000])
                elif event.get('type') == 'error':
                    errors.append(json.dumps({k: event[k] for k in ('code', 'message', 'error') if k in event})[:8000])
                errors = errors[-20:]
    # CLI-level diagnostics, not item.command_execution stdout/stderr.
    if stderr_path.exists():
        with stderr_path.open('rb') as source:
            source.seek(max(0, stderr_path.stat().st_size - 32000))
            errors.append(source.read(32000).decode('utf-8', errors='replace'))
    text = '\n'.join(errors).casefold()
    if any(p in text for p in ('usage_limit_reached', 'usage limit', "out of codex", 'insufficient_quota',
                                'weekly limit', 'quota_exceeded')):
        return 'quota'
    if any(p in text for p in ('rate_limit_exceeded', 'too many requests', 'http 429')):
        return 'rate_limited'
    if any(p in text for p in ('authentication_error', 'unauthorized', 'invalid_api_key', 'token expired')):
        return 'authentication'
    if any(p in text for p in ('sandbox', 'landlock', 'seccomp')):
        return 'sandbox'
    if any(p in text for p in ('model_not_found', 'unsupported model', 'invalid reasoning', 'unknown config')):
        return 'configuration'
    return 'failed'


def error_kind(exc: BaseException) -> str:
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ProviderPaused):
            return 'quota'
        if isinstance(current, Deferred):
            return 'deferred'
        category = getattr(current, 'status', None)
        if category in {'quota', 'rate_limited', 'authentication', 'sandbox', 'configuration', 'timed_out'}:
            return category
        current = current.__cause__ or current.__context__
    return 'failed'


@contextmanager
def admission(config, artifacts: Path, stage: str | None = None):
    routed = resolve(config, artifacts, stage)
    if not isinstance(routed, ExecutionConfig):
        yield routed, ()
        return
    settings = routed.hydra_settings
    if routed.hydra_home is None or settings is None:
        raise Error('Hydra execution configuration is incomplete.')
    home = routed.hydra_home
    db = connect(home)
    slot = None
    identity = launch_id(artifacts)
    launched = False
    status = 'completed'
    try:
        first = settings.reserved_core_slots if routed.hydra_role == 'scout' else 0
        deadline = time.monotonic() + 30
        while slot is None:
            if health(db):
                raise ProviderPaused('Shared Codex provider usage is paused; resume after the actual reset.')
            if (home / 'PAUSED').exists():
                raise Deferred('Maintenance is paused; no model was launched.')
            for index in range(first, settings.max_parallel):
                handle = (home / 'locks' / f'hydra-slot-{index}.lock').open('a+')
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                slot = handle
                break
            if slot is None:
                worker_state(db, routed.hydra_actor, routed.hydra_role, 'waiting_capacity', pid=os.getpid(),
                             stage=routed.execution_stage, detail='Waiting for an execution slot; no model usage yet.')
                if time.monotonic() >= deadline:
                    raise Deferred('Execution capacity is busy; task remains queued.')
                time.sleep(.25)
        with immediate(db):
            if health(db):
                raise ProviderPaused('Shared provider usage is paused.')
            cap = routed.max_runs_per_day
            if cap and used_today(db) >= cap:
                raise Deferred('Local daily launch cap reached. No model was launched.')
            if routed.execution_stage == 'escalate':
                if used_today(db, 'escalate') >= settings.max_escalations_per_day:
                    raise Deferred('Daily escalation cap reached; candidate remains queued.')
            if db.execute('SELECT id FROM hydra_launches WHERE id=?', (identity,)).fetchone():
                raise Error('This invocation already has a launch record; refusing to replay it.')
            db.execute('INSERT INTO hydra_launches(id,actor,stage,model,effort,started_at,status,artifact_path) '
                       "VALUES (?,?,?,?,?,?,'running',?)",
                       (identity, routed.hydra_actor, routed.execution_stage, routed.model,
                        routed.reasoning_effort, utcnow(), str(artifacts)))
            launched = True
        worker_state(db, routed.hydra_actor, routed.hydra_role, 'running', pid=os.getpid(),
                     stage=routed.execution_stage, model=routed.model, effort=routed.reasoning_effort,
                     run_id=artifacts.name)
        print(f'{routed.execution_stage}: model={routed.model} effort={routed.reasoning_effort} '
              f'run={artifacts.name}', flush=True)
        try:
            yield routed, (slot.fileno(), *routed.inherited_fds)
        except BaseException as exc:
            status = 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else error_kind(exc)
            if status == 'quota':
                provider_pause(db, 'quota', 'Codex reported exhausted usage. New launches paused; no fallback.')
            raise
        finally:
            if launched:
                from .codex import events
                usage = events(artifacts / 'events.jsonl')['usage']
                with db:
                    db.execute('UPDATE hydra_launches SET finished_at=?,status=?,usage=? WHERE id=?',
                               (utcnow(), status, json.dumps(usage), identity))
    finally:
        try:
            if not launched:
                # Legacy callers set invoked=1 before entering the adapter. A denied
                # admission is not a model launch, including in imported history.
                table, _, row_id = identity.partition(':')
                if table in ('runs', 'thread_turns', 'implementation_steps', 'review_turns'):
                    if not db.execute('SELECT id FROM hydra_launches WHERE id=?', (identity,)).fetchone():
                        with db:
                            db.execute(f'UPDATE {table} SET invoked=0 WHERE id=?', (row_id,))
        finally:
            if slot is not None:
                slot.close()
            db.close()
