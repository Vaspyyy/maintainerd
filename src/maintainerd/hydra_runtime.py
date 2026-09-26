"""Failure-isolated Hydra workers and a nonblocking, prefixed log supervisor."""
from __future__ import annotations

import codecs
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

from .hydra_execution import error_kind
from .hydra_pool import pending_count
from .hydra_state import HydraState, health, provider_pause, worker_state
from .hydra_tasks import scout_once, validate_once
from .state import Error


def worker_loop(state: HydraState, repository: str, actor: str, role: str, *,
                index: int = 0, total: int = 1, once: bool = False) -> int:
    settings = state.config.hydra_settings
    lock_name = 'serve-' + actor if role == 'core' else 'hydra-worker-' + actor
    last_state, failures, next_explore = '', 0, 0.0
    with state.lock(lock_name):
        print(f'{role} worker online; no GitHub identity' if role == 'scout'
              else 'core worker online; existing issues, PRs and review obligations retained', flush=True)
        while True:
            circuit = health(state.db)
            if circuit or (state.home / 'PAUSED').exists() or not state.can_run():
                label = 'paused_provider' if circuit else 'paused_local'
                worker_state(state.db, actor, role, label, pid=os.getpid(),
                             detail='No new model launches; existing queue and PRs are retained.')
                if last_state != label:
                    print(f'{label}: no new calls. ' + ('Run hydra resume after the provider reset.'
                                                       if circuit else 'Waiting for local pause/budget to clear.'), flush=True)
                    last_state = label
                if once:
                    return 75
                time.sleep(15)
                continue
            try:
                worker_state(state.db, actor, role, 'checking', pid=os.getpid())
                if role == 'scout':
                    if pending_count(state, repository) >= settings.queue_limit:
                        worker_state(state.db, actor, role, 'backpressure', pid=os.getpid(),
                                     detail='Private finding queue is full; waiting for validation.')
                        if once:
                            return 0
                        time.sleep(settings.poll_seconds)
                        continue
                    scout_once(state, repository, actor, index, total)
                    delay = settings.scout_interval_seconds
                else:
                    from . import discussion
                    # Existing author revisions, implementation, discussion and peer review win.
                    activity = discussion.sync_once(state, actor, max_threads=1)
                    if not activity:
                        activity = int(validate_once(state, actor, repository))
                    if not activity and settings.core_explore and time.monotonic() >= next_explore:
                        from . import cycle
                        cycle.wake(state, actor, 'hydra-core-exploration')
                        next_explore = time.monotonic() + settings.core_explore_seconds
                    delay = settings.poll_seconds
                failures, last_state = 0, 'idle'
                worker_state(state.db, actor, role, 'idle', pid=os.getpid(), detail=f'Next check in {delay}s.')
                if once:
                    return 0
                time.sleep(delay)
            except KeyboardInterrupt:
                worker_state(state.db, actor, role, 'stopped', pid=os.getpid(), detail='Operator stopped worker.')
                raise
            except Exception as exc:
                kind = error_kind(exc)
                if kind == 'quota':
                    provider_pause(state.db, 'quota', 'Codex usage exhausted. No credential/model fallback.')
                    print('quota: shared provider circuit paused; other in-flight turns may finish.', flush=True)
                    if once:
                        return 75
                    continue
                if kind == 'deferred':
                    worker_state(state.db, actor, role, 'waiting', pid=os.getpid(), detail=str(exc))
                    if once:
                        return 75
                    time.sleep(settings.poll_seconds)
                    continue
                if kind == 'rate_limited' and failures < settings.max_transient_retries:
                    failures += 1
                    worker_state(state.db, actor, role, 'cooldown', pid=os.getpid(),
                                 detail=f'Transient rate limit, bounded retry {failures}/{settings.max_transient_retries}.')
                    print(f'rate_limited: cooling down {settings.rate_limit_cooldown_seconds}s '
                          f'(retry {failures}/{settings.max_transient_retries})', flush=True)
                    if once:
                        return 75
                    time.sleep(settings.rate_limit_cooldown_seconds)
                    continue
                detail = str(exc) if isinstance(exc, Error) else type(exc).__name__
                worker_state(state.db, actor, role, 'failed', pid=os.getpid(), detail=f'{kind}: {detail}')
                print(f'worker halted ({kind}): {detail}. Other workers continue; no automatic fallback.', flush=True)
                return 1


def worker_argv(home: Path, repository: str, actor: str, role: str, index: int, total: int) -> list[str]:
    return [sys.executable, '-m', 'maintainerd.hydra_cli', '--home', str(home), 'hydra', '_worker',
            '--repo', repository, '--actor', actor, '--role', role,
            '--index', str(index), '--total', str(total)]


class LineMux:
    """Use os.read rather than TextIO.readline so buffered lines never disappear behind select."""
    def __init__(self):
        self.decoders = {}
        self.buffers = {}

    def feed(self, actor: str, data: bytes, final: bool = False) -> list[str]:
        decoder = self.decoders.setdefault(actor, codecs.getincrementaldecoder('utf-8')('replace'))
        text = self.buffers.get(actor, '') + decoder.decode(data, final=final)
        parts = text.split('\n')
        pending = parts.pop()
        if len(pending) > 16000:
            parts.append(pending[:16000] + ' [long log line truncated]')
            pending = ''
        if final and pending:
            parts.append(pending)
            pending = ''
        self.buffers[actor] = pending
        return [f'[{actor}] {line.rstrip()}' for line in parts]


def stop_workers(workers: dict[str, subprocess.Popen]) -> None:
    for process in workers.values():
        if process.poll() is None:
            process.terminate()  # Workers translate SIGTERM to KeyboardInterrupt and stop Codex groups.
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and any(p.poll() is None for p in workers.values()):
        time.sleep(.1)
    for process in workers.values():
        if process.poll() is None:
            process.kill()
    for process in workers.values():
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def supervise(state: HydraState, repository: str, maintainers: list[str], scouts: int) -> int:
    from .cli import _serve_identity_check
    for maintainer in maintainers:
        row = state.one('maintainers', maintainer)
        if row['repository'] != repository:
            raise Error('Hydra v1 serves one repository per supervisor; Atlas is not enabled.')
    _serve_identity_check(state, maintainers)
    actors = [(m, 'core', 0, 1) for m in maintainers]
    actors += [(f'scout-{i + 1:03}', 'scout', i, scouts) for i in range(scouts)]
    if len({a[0] for a in actors}) != len(actors):
        raise Error('A maintainer name conflicts with a private scout name.')
    settings = state.config.hydra_settings
    if scouts and settings.reserved_core_slots >= settings.max_parallel:
        raise Error('The execution-slot policy leaves no capacity for scouts.')
    workers, active = {}, set()
    selector, mux = selectors.DefaultSelector(), LineMux()
    failed = False
    with state.lock('hydra-supervisor'):
        print(f'Hydra v1 | {repository} | {len(maintainers)} core + {scouts} private scouts | '
              f'max {settings.max_parallel} simultaneous Codex turns '
              f'({settings.reserved_core_slots} slots reserved for core)', flush=True)
        print('Ctrl+C stops this fleet. Worker failures are isolated; shared quota pauses new calls.', flush=True)
        try:
            for actor, role, index, total in actors:
                process = subprocess.Popen(worker_argv(state.home, repository, actor, role, index, total),
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           env=dict(os.environ, PYTHONUNBUFFERED='1'), start_new_session=True)
                workers[actor] = process
                active.add(actor)
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ, actor)
                print(f'[{actor}] started {role} worker pid={process.pid}', flush=True)
            while active:
                for key, _ in selector.select(timeout=.5):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    for line in mux.feed(key.data, chunk, final=not chunk):
                        print(line, flush=True)
                    if not chunk:
                        selector.unregister(key.fileobj)
                for actor in list(active):
                    code = workers[actor].poll()
                    if code is None:
                        continue
                    active.remove(actor)
                    failed |= code != 0
                    print(f'[{actor}] exited {code}; {len(active)} other worker(s) remain. '
                          'Use hydra status for the saved reason.', flush=True)
        finally:
            stop_workers(workers)
            selector.close()
            for process in workers.values():
                if process.stdout:
                    process.stdout.close()
    return 1 if failed else 0
