"""Hydra's opt-in CLI; other maintainerd commands retain their existing behavior."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

from .hydra_config import STAGES, Settings, template
from .hydra_state import HydraState, health, used_today
from .state import Error, State, default_home, name, utcnow


def parser():
    root = argparse.ArgumentParser(prog='maintainerd hydra', description='Private scouts, validated core, bounded escalation.')
    commands = root.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create hydra.toml without overwriting existing settings')
    init.add_argument('--scout-model', default='gpt-6-luna')
    init.add_argument('--core-model', default='gpt-6-sol')
    init.add_argument('--escalation-model', default='gpt-6-astra')
    commands.add_parser('profiles', help='Show requested model and reasoning effort for every stage')
    status = commands.add_parser('status', help='Show live workers, queue and observed launch/token usage')
    status.add_argument('--json', action='store_true')
    pool = commands.add_parser('pool', help='Inspect private candidate decisions and provenance')
    pool.add_argument('id', nargs='?')
    pool.add_argument('--json', action='store_true')
    serve = commands.add_parser('serve', help='Run the scout swarm and existing maintainers together')
    serve.add_argument('maintainers', nargs='+')
    serve.add_argument('--repo')
    serve.add_argument('--scouts', type=int)
    commands.add_parser('resume', help='Clear the local provider pause AFTER actual provider usage has reset')
    scout = commands.add_parser('scout', help='Run one private read-only scout turn, without a GitHub identity')
    scout.add_argument('--repo', required=True)
    scout.add_argument('--name', default='scout-manual')
    validate = commands.add_parser('validate', help='Run one core validation/promotion from the private queue')
    validate.add_argument('maintainer')
    worker = commands.add_parser('_worker', help=argparse.SUPPRESS)
    worker.add_argument('--repo', required=True)
    worker.add_argument('--actor', required=True)
    worker.add_argument('--role', choices=('core', 'scout'), required=True)
    worker.add_argument('--index', type=int, default=0)
    worker.add_argument('--total', type=int, default=1)
    worker.add_argument('--once', action='store_true')
    return root


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status_data(state: HydraState) -> dict:
    workers = state.rows('SELECT * FROM hydra_workers ORDER BY role,actor')
    for item in workers:
        item['process_alive'] = _alive(item['pid'])
        if not item['process_alive'] and item['state'] in ('running', 'checking', 'idle', 'cooldown', 'waiting_capacity'):
            item['state'] = 'stopped_or_lost'
    stages = state.rows('SELECT stage,count(*) AS launches FROM hydra_launches '
                        "WHERE substr(started_at,1,10)=substr(?,1,10) GROUP BY stage", (utcnow(),))
    tokens = {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}
    for row in state.rows("SELECT usage FROM hydra_launches WHERE usage IS NOT NULL AND substr(started_at,1,10)=date('now')"):
        try:
            usage = json.loads(row['usage'])
            # Imported legacy records wrap usage together with thread metadata.
            if isinstance(usage.get('usage'), dict):
                usage = usage['usage']
            for key in tokens:
                value = usage.get(key, 0)
                if type(value) is int and value >= 0:
                    tokens[key] += value
        except (ValueError, TypeError, AttributeError):
            continue
    return {
        'workers': workers, 'provider_pause': health(state.db), 'local_launches_today': used_today(state.db),
        'remaining_local_starts': state.remaining(), 'launches_by_stage': stages,
        'observed_tokens_today': tokens,
        'queue': state.rows('SELECT repository,status,count(*) AS count FROM hydra_candidates GROUP BY repository,status'),
        'note': 'Tokens are observed CLI events, not subscription percentages or API billing estimates. Cached input is a subset of input.',
    }


def dispatch(args, state: HydraState) -> int:
    if args.command == 'profiles':
        for stage in STAGES:
            profile = state.config.hydra_settings.profiles.get(stage)
            print(f'{stage:10} {profile.model + " / " + profile.reasoning_effort if profile else "disabled"}')
        print('These are requested models. Live CLI access and supported effort levels are checked by Codex at execution.')
    elif args.command == 'status':
        data = status_data(state)
        if args.json:
            print(json.dumps(data, indent=2))
            return 0
        print(f'Hydra | local launches today: {data["local_launches_today"]} | '
              f'remaining: {data["remaining_local_starts"] if data["remaining_local_starts"] is not None else "unlimited"}')
        print('Provider: PAUSED' if data['provider_pause'] else 'Provider: no reported quota pause')
        for w in data['workers']:
            print(f'{w["actor"]:16} {w["state"]:18} {w["stage"] or "-":9} '
                  f'{w["model"] or "-"} {w["effort"] or ""} | {w["detail"] or ""}')
        print('Private queue: ' + (', '.join(f'{x["repository"]}/{x["status"]}={x["count"]}' for x in data['queue']) or 'empty'))
        print(data['note'])
    elif args.command == 'pool':
        if args.id:
            rows = state.rows('SELECT * FROM hydra_candidates WHERE id=?', (args.id,))
            if not rows:
                raise Error('Unknown private candidate ID.')
            item = rows[0]
            item['payload'] = json.loads(item['payload'])
            item['observations'] = state.rows('SELECT actor,source_sha,created_at FROM hydra_observations WHERE candidate_id=?', (args.id,))
            print(json.dumps(item, indent=2))
        else:
            rows = state.rows('SELECT id,status,owner,payload,issue_url FROM hydra_candidates ORDER BY updated_at DESC LIMIT 100')
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for row in rows:
                    print(f'{row["id"]}  {row["status"]:12}  {row["owner"] or "unclaimed":12}  '
                          f'{json.loads(row["payload"])["title"]}')
                if not rows:
                    print('Private finding pool is empty.')
    elif args.command == 'resume':
        with state.db:
            state.db.execute("DELETE FROM hydra_health WHERE key='provider'")
        print('Local provider pause cleared. This does NOT reset Codex allowance or repair failed workers.')
    elif args.command == 'scout':
        from .hydra_tasks import scout_once
        state.one('repositories', args.repo)
        scout_once(state, args.repo, name(args.name))
    elif args.command == 'validate':
        from .hydra_tasks import validate_once
        repository = state.one('maintainers', args.maintainer)['repository']
        if not validate_once(state, args.maintainer, repository):
            print('No private candidate currently needs this core turn.')
    elif args.command == '_worker':
        from .hydra_runtime import worker_loop
        return worker_loop(state, args.repo, args.actor, args.role, index=args.index, total=args.total, once=args.once)
    elif args.command == 'serve':
        from .hydra_runtime import supervise
        maintainers = list(dict.fromkeys(name(n) for n in args.maintainers))
        repository = args.repo or state.one('maintainers', maintainers[0])['repository']
        state.one('repositories', repository)
        scouts = state.config.hydra_settings.scouts if args.scouts is None else args.scouts
        if not 0 <= scouts <= 128:
            raise Error('--scouts must be from 0 to 128; worker count is not the concurrency limit.')
        return supervise(state, repository, maintainers, scouts)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    prefix = argparse.ArgumentParser(add_help=False)
    prefix.add_argument('--home', type=Path, default=default_home())
    location, rest = prefix.parse_known_args(argv)
    if not rest or rest[0] != 'hydra':
        from .cli import main as legacy_main
        return legacy_main(argv)
    args = parser().parse_args(rest[1:])
    os.umask(0o077)
    if not sys.platform.startswith('linux'):
        print('Hydra currently requires Linux.', file=sys.stderr)
        return 1
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    state = None
    try:
        if args.command == 'init':
            state = State(location.home)
            content = template(args.scout_model, args.core_model, args.escalation_model or None)
            path = state.home / 'hydra.toml'
            try:
                with path.open('x', encoding='utf-8') as output:
                    output.write(content)
            except FileExistsError as exc:
                raise Error('hydra.toml already exists; it was not overwritten.') from exc
            print(f'Created {path}. Existing config.toml, App keys, identities and PR state were not changed.')
            print('Review model/effort profiles and limits, then run: maintainerd hydra profiles')
            return 0
        settings = Settings.load(location.home.expanduser().resolve() / 'hydra.toml')
        actor = name(args.actor) if args.command == '_worker' else (
            name(args.name) if args.command == 'scout' else (
                name(args.maintainer) if args.command == 'validate' else 'operator'))
        role = args.role if args.command == '_worker' else ('scout' if args.command == 'scout' else 'core')
        state = HydraState(location.home, settings, actor, role)
        return dispatch(args, state)
    except KeyboardInterrupt:
        print('Hydra stopped. Active controllers were asked to terminate their Codex process groups.', file=sys.stderr)
        return 130
    except (Error, OSError, sqlite3.Error) as exc:
        print(f'Hydra error: {exc}', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
        if state is not None:
            state.close()


if __name__ == '__main__':
    raise SystemExit(main())
