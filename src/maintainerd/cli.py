"""Small command surface; no web server, hosted API, or background installation."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import subprocess
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, codex, cycle, discussion, publisher, repo
from .state import Error, State, default_home, name, utcnow


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="maintainerd", description="Persistent autonomous maintainer experiments.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", type=Path, default=default_home(), help="Dedicated data directory (or MAINTAINERD_HOME)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create local configuration and SQLite state")
    commands.add_parser("doctor", help="Check tools and subscription login, without calling a model")
    repositories = commands.add_parser("repo", help="Register controller-owned repository copies").add_subparsers(dest="action", required=True)
    add_repo = repositories.add_parser("add")
    add_repo.add_argument("source", help="HTTPS/SSH repository URL or local checkout")
    add_repo.add_argument("--name", dest="alias")
    add_repo.add_argument("--branch", help="Defaults to repository HEAD")
    add_repo.add_argument("--github", help="owner/repo, useful with a local source checkout")
    repositories.add_parser("list")
    maintainers = commands.add_parser("maintainer", help="Contributor identities, not fixed roles").add_subparsers(dest="action", required=True)
    create = maintainers.add_parser("create")
    create.add_argument("name")
    create.add_argument("--repo", dest="repository", help="Optional when one repository is registered")
    create.add_argument("--mission", default=cycle.MISSION)
    maintainers.add_parser("list")
    identities = commands.add_parser(
        "identity", help="Optional per-maintainer GitHub App identities"
    ).add_subparsers(dest="action", required=True)
    identity_set = identities.add_parser("set")
    identity_set.add_argument("name")
    identity_set.add_argument("--app-id", type=int, required=True)
    identity_set.add_argument("--key-path", required=True)
    identity_clear = identities.add_parser("clear")
    identity_clear.add_argument("name")
    identities.add_parser("list")
    wake = commands.add_parser("wake", help="One finite maintenance cycle")
    wake.add_argument("name")
    wake.add_argument("--reason", choices=["exploration", "maintenance", "scheduled"], default="exploration")
    wake.add_argument("--dry-run", action="store_true", help="Prepare context without calling Codex")
    wake.add_argument("--keep-worktree", action="store_true")
    runs = commands.add_parser("runs", help="Recent local runs")
    runs.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help="Print a report and its artifact location")
    show.add_argument("run", nargs="?", default="latest")
    show.add_argument("--json", action="store_true")
    publish = commands.add_parser("publish", help="Route one completed proposal into GitHub discussion")
    publish.add_argument("run", nargs="?", default="latest")
    inbox = commands.add_parser(
        "inbox", help="Fetch comments, approvals, and continue one implementation step"
    )
    inbox.add_argument("name")
    inbox.add_argument("--max-threads", type=int, default=2)
    commands.add_parser("implementations", help="List claimed implementation work and draft PRs")
    memories = commands.add_parser("memory", help="Inspect observations or add explicit human guidance").add_subparsers(dest="action", required=True)
    add_note = memories.add_parser("add")
    add_note.add_argument("name")
    add_note.add_argument("text")
    list_notes = memories.add_parser("list")
    list_notes.add_argument("name")
    forget = memories.add_parser("forget")
    forget.add_argument("name")
    forget.add_argument("id", type=int)
    commands.add_parser("pause", help="Prevent new real runs; does not interrupt an active run")
    commands.add_parser("resume", help="Permit new real runs")
    serve = commands.add_parser(
        "serve",
        help="Foreground loop for one or more maintainers: discussion polling plus exploration",
    )
    serve.add_argument("names", nargs="+", metavar="name")
    cadence = serve.add_mutually_exclusive_group()
    cadence.add_argument("--every-hours", type=float)
    cadence.add_argument("--every-minutes", type=float)
    serve.add_argument("--poll-seconds", type=int, default=300)
    worker = commands.add_parser("_serve-worker", help=argparse.SUPPRESS)
    worker.add_argument("name")
    worker.add_argument("--interval-seconds", type=float, required=True)
    worker.add_argument("--poll-seconds", type=int, required=True)
    return root


def doctor(state: State) -> int:
    failed = False
    print(
        f"Data: {state.home}\nPython: {sys.version.split()[0]}\n"
        "Mode: read-only exploration/discussion; approved implementation uses workspace-write"
    )
    try:
        print("PASS " + repo.git(["--version"]).strip())
    except Error as exc:
        print(f"FAIL {exc}")
        failed = True
    try:
        print("PASS " + codex.preflight(state.config) + "; ChatGPT authentication confirmed")
    except Error as exc:
        print(f"FAIL {exc}")
        failed = True
    if shutil.which("gh"):
        try:
            repo.command(["gh", "auth", "status", "--hostname", "github.com"], timeout=20)
            print("PASS gh login available for optional read-only GitHub snapshots")
        except Error:
            print("WARN gh is installed but login was not confirmed; use 'gh auth login'.")
    else:
        print("WARN gh not found; repository inspection works but live issue/PR/CI context will be missing.")
    identities = state.rows(
        "SELECT m.name,r.github FROM maintainers m "
        "JOIN repositories r ON r.name=m.repository ORDER BY m.name"
    )
    bot_owners: dict[str, list[str]] = {}
    if not identities:
        if publisher.configured(state.config):
            print("INFO GitHub App configured; create a maintainer to verify its repository installation.")
        else:
            print("INFO No GitHub maintainer identity configured yet.")
    for item in identities:
        if not item["github"]:
            print(f"WARN {item['name']} has no GitHub owner/repo configured.")
            continue
        if not publisher.configured_for(state, item["name"]):
            message = f"{item['name']} has no GitHub App identity"
            if state.config.publish_proposals:
                print("FAIL " + message)
                failed = True
            else:
                print("INFO " + message + "; local reports still work.")
            continue
        try:
            active = publisher.session_for(state, item["name"], item["github"])
            config = publisher.config_for(state, item["name"])
            implementation_ready = (
                active.permissions.get("contents") == "write"
                and active.permissions.get("pull_requests") == "write"
            )
            capability = (
                "issue discussions + draft PR implementation"
                if implementation_ready else
                "issue discussions only"
            )
            print(
                f"PASS GitHub App {config.github_app_id} ({active.bot_login}) can write "
                f"{capability} in {item['github']} for {item['name']}"
            )
            bot_owners.setdefault(active.bot_login, []).append(item["name"])
        except Error as exc:
            print(f"FAIL {item['name']}: {exc}")
            failed = True
    for bot_login, owners in bot_owners.items():
        if len(owners) > 1:
            print(
                f"WARN {', '.join(owners)} share {bot_login}; give them distinct Apps "
                "before expecting bot-to-bot conversation."
            )
    remaining = state.remaining()
    if remaining is None:
        print("Remaining run starts today (UTC): unlimited local cap")
    else:
        print(f"Remaining run starts today (UTC): {remaining}/{state.config.max_runs_per_day}")
    print("Doctor does not call a model or prove sandbox enforcement. The first wake is the live integration check.")
    print("Use trusted repositories only; the Codex sandbox is not a separate VM or protection from reading your home.")
    return int(failed)


def _serve_interval_seconds(hours: float | None, minutes: float | None) -> float:
    if hours is None and minutes is None:
        return 12 * 60 * 60
    if minutes is not None:
        if not 1 <= minutes <= 10080:
            raise Error("--every-minutes must be between 1 and 10080.")
        return minutes * 60
    assert hours is not None
    if not (1 / 60) <= hours <= 168:
        raise Error("--every-hours must be between 1/60 and 168.")
    return hours * 60 * 60


def _serve_identity_check(state: State, maintainers: list[str]) -> None:
    if len(maintainers) < 2:
        return
    logins: dict[str, str] = {}
    for maintainer in maintainers:
        item = state.one("maintainers", maintainer)
        repository = state.one("repositories", item["repository"])
        target = repository.get("github")
        if not target:
            raise Error(f"{maintainer} has no GitHub owner/repo configured.")
        if not publisher.configured_for(state, maintainer):
            raise Error(
                f"{maintainer} needs its own GitHub App identity before multi-maintainer serve."
            )
        active = publisher.session_for(state, maintainer, target)
        previous = logins.get(active.bot_login)
        if previous:
            raise Error(
                f"{previous} and {maintainer} both resolve to {active.bot_login}. "
                "Multi-maintainer serve requires distinct GitHub App identities."
            )
        logins[active.bot_login] = maintainer


def _exploration_delay(state: State, maintainer: str, interval_seconds: float) -> float:
    rows = state.rows(
        "SELECT started_at FROM runs WHERE maintainer=? AND invoked=1 "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (maintainer,),
    )
    if not rows:
        return 0.0
    due = datetime.fromisoformat(rows[0]["started_at"]) + timedelta(seconds=interval_seconds)
    return (due - datetime.now(timezone.utc)).total_seconds()


def _serve_worker_loop(
    state: State,
    maintainer: str,
    interval_seconds: float,
    poll_seconds: int,
) -> None:
    if not 60 <= interval_seconds <= 168 * 60 * 60:
        raise Error("Exploration interval must be between 1 minute and 168 hours.")
    if not 15 <= poll_seconds <= 3600:
        raise Error("--poll-seconds must be between 15 and 3600.")
    state.one("maintainers", maintainer)
    cadence = (
        f"{interval_seconds / 60:g}m"
        if interval_seconds < 3600
        else f"{interval_seconds / 3600:g}h"
    )
    with state.lock(f"serve-{maintainer}") as _serve_lock_fd:
        print(
            f"worker online: explore every {cadence}, poll every {poll_seconds}s",
            flush=True,
        )
        next_poll = 0.0
        while True:
            if (state.home / "PAUSED").exists():
                time.sleep(15)
                continue

            monotonic = time.monotonic()
            if monotonic >= next_poll:
                discussion.sync_once(state, maintainer)
                next_poll = time.monotonic() + poll_seconds

            active_implementation = state.rows(
                "SELECT id FROM implementations WHERE maintainer=? "
                "AND status IN ('draft','working') LIMIT 1",
                (maintainer,),
            )
            if active_implementation:
                time.sleep(min(15.0, max(0.25, next_poll - time.monotonic())))
                continue

            delay = _exploration_delay(state, maintainer, interval_seconds)
            if delay <= 0 and state.can_run():
                cycle.wake(state, maintainer, "scheduled")
                continue

            waits = [max(0.25, next_poll - time.monotonic())]
            if state.can_run():
                waits.append(max(0.25, delay))
            time.sleep(min(15.0, *waits))


def _worker_argv(
    state: State,
    maintainer: str,
    interval_seconds: float,
    poll_seconds: int,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "maintainerd",
        "--home",
        str(state.home),
        "_serve-worker",
        maintainer,
        "--interval-seconds",
        str(interval_seconds),
        "--poll-seconds",
        str(poll_seconds),
    ]


def _stop_workers(workers: dict[str, subprocess.Popen]) -> None:
    for process in workers.values():
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(process.poll() is not None for process in workers.values()):
            return
        time.sleep(0.1)
    for process in workers.values():
        if process.poll() is None:
            process.kill()
    for process in workers.values():
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def serve(
    state: State,
    maintainers: list[str] | str,
    interval_seconds: float,
    poll_seconds: int,
) -> None:
    if isinstance(maintainers, str):
        maintainers = [maintainers]
    maintainers = list(dict.fromkeys(maintainers))
    if not maintainers:
        raise Error("serve needs at least one maintainer.")
    if not 60 <= interval_seconds <= 168 * 60 * 60:
        raise Error("Exploration interval must be between 1 minute and 168 hours.")
    if not 15 <= poll_seconds <= 3600:
        raise Error("--poll-seconds must be between 15 and 3600.")
    for maintainer in maintainers:
        state.one("maintainers", maintainer)
    _serve_identity_check(state, maintainers)

    cadence = (
        f"{interval_seconds / 60:g}m"
        if interval_seconds < 3600
        else f"{interval_seconds / 3600:g}h"
    )
    budget = "unlimited" if state.remaining() is None else f"{state.remaining()} starts left today"
    print(
        f"Parallel foreground supervisor: {len(maintainers)} worker(s), "
        f"explore each every {cadence}, poll every {poll_seconds}s; "
        f"local budget {budget}. Ctrl+C stops all workers.",
        flush=True,
    )

    selector = selectors.DefaultSelector()
    workers: dict[str, subprocess.Popen] = {}
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        for maintainer in maintainers:
            process = subprocess.Popen(
                _worker_argv(state, maintainer, interval_seconds, poll_seconds),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,
            )
            if process.stdout is None:
                raise Error(f"Could not capture {maintainer} worker output.")
            workers[maintainer] = process
            selector.register(process.stdout, selectors.EVENT_READ, maintainer)
            print(f"[{maintainer}] worker started (pid {process.pid})", flush=True)

        while True:
            for key, _mask in selector.select(timeout=0.5):
                stream = key.fileobj
                maintainer = key.data
                line = stream.readline()
                if line:
                    print(f"[{maintainer}] {line.rstrip()}", flush=True)
                else:
                    try:
                        selector.unregister(stream)
                    except Exception:
                        pass

            for maintainer, process in workers.items():
                returncode = process.poll()
                if returncode is None:
                    continue
                # Drain anything that arrived between the last select and exit.
                if process.stdout is not None:
                    for line in process.stdout:
                        print(f"[{maintainer}] {line.rstrip()}", flush=True)
                raise Error(
                    f"{maintainer} worker exited with status {returncode}; "
                    "stopping the parallel supervisor."
                )
    finally:
        selector.close()
        _stop_workers(workers)


def dispatch(args: argparse.Namespace, state: State) -> int:
    match args.command:
        case "init":
            print(f"Ready: {state.home}\nConfiguration: {state.home / 'config.toml'}")
        case "doctor":
            return doctor(state)
        case "repo":
            if args.action == "add":
                with state.lock():
                    alias = repo.add(state, args.source, args.alias, args.branch, args.github)
                print(f"Registered {alias}. Your source repository was not modified.")
            else:
                for item in state.rows("SELECT * FROM repositories ORDER BY name"):
                    print(f"{item['name']}  [{item['branch']}]  {item['source']}")
        case "maintainer":
            if args.action == "create":
                name(args.name)
                if not args.mission.strip() or len(args.mission) > 4000:
                    raise Error("Mission must contain 1-4000 characters.")
                repository = args.repository
                if not repository:
                    choices = state.rows("SELECT name FROM repositories")
                    if len(choices) != 1:
                        raise Error("Register a repository first, or select one with --repo NAME.")
                    repository = choices[0]["name"]
                state.one("repositories", repository)
                with state.lock(), state.db:
                    previous = state.rows("SELECT * FROM maintainers WHERE name=?", (args.name,))
                    if previous and (previous[0]["repository"] != repository or previous[0]["mission"] != args.mission):
                        raise Error("That maintainer already exists with different settings.")
                    state.db.execute("INSERT OR IGNORE INTO maintainers VALUES (?,?,?)", (args.name, repository, args.mission))
                print(f"Maintainer {args.name} watches {repository}. Nothing is scheduled automatically.")
            else:
                for item in state.rows("SELECT * FROM maintainers ORDER BY name"):
                    print(f"{item['name']}  ->  {item['repository']}")
        case "identity":
            if args.action == "list":
                rows = state.rows(
                    "SELECT maintainer,github_app_id,github_private_key_path "
                    "FROM maintainer_identities ORDER BY maintainer"
                )
                for item in rows:
                    print(
                        f"{item['maintainer']}  app={item['github_app_id']}  "
                        f"key={item['github_private_key_path']}"
                    )
                if not rows:
                    print("No per-maintainer identities; configured maintainers use the global App fallback.")
            else:
                state.one("maintainers", args.name)
                with state.lock(), state.db:
                    if args.action == "clear":
                        state.db.execute(
                            "DELETE FROM maintainer_identities WHERE maintainer=?", (args.name,)
                        )
                        print(f"{args.name} now uses the global GitHub App fallback.")
                    else:
                        if args.app_id <= 0:
                            raise Error("--app-id must be a positive integer.")
                        key_path = str(Path(args.key_path).expanduser().resolve())
                        if "\n" in key_path:
                            raise Error("--key-path must be a filesystem path.")
                        state.db.execute(
                            "INSERT INTO maintainer_identities("
                            "maintainer,github_app_id,github_private_key_path"
                            ") VALUES (?,?,?) ON CONFLICT(maintainer) DO UPDATE SET "
                            "github_app_id=excluded.github_app_id,"
                            "github_private_key_path=excluded.github_private_key_path",
                            (args.name, args.app_id, key_path),
                        )
                        print(f"Saved GitHub App identity for {args.name}. Run doctor to verify it.")
        case "wake":
            cycle.wake(state, args.name, args.reason, dry_run=args.dry_run, keep_worktree=args.keep_worktree)
        case "runs":
            if not 1 <= args.limit <= 200:
                raise Error("--limit must be between 1 and 200.")
            rows = state.rows("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (args.limit,))
            for item in rows:
                print(f"{item['id']}  {item['status']:12s}  {item['maintainer']}  {item['started_at']}")
            if not rows:
                print("No runs yet.")
        case "show":
            run_id = args.run
            if run_id == "latest":
                rows = state.rows("SELECT id FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1")
                if not rows:
                    raise Error("No runs yet.")
                run_id = rows[0]["id"]
            item = state.one("runs", run_id)
            artifacts = state.home / "runs" / item["id"]
            if args.json:
                for key in ("result", "usage"):
                    item[key] = json.loads(item[key]) if item[key] else None
                print(json.dumps(item, indent=2))
            elif (artifacts / "report.md").is_file():
                print((artifacts / "report.md").read_text(encoding="utf-8"))
            else:
                print(f"Run {run_id}: {item['status']}. {item['error'] or 'No model report was generated.'}")
            print(f"Artifacts: {artifacts}", file=sys.stderr if args.json else sys.stdout)
        case "publish":
            run_id = args.run
            if run_id == "latest":
                rows = state.rows(
                    "SELECT id FROM runs WHERE status='completed' AND result IS NOT NULL "
                    "ORDER BY started_at DESC, rowid DESC LIMIT 1"
                )
                if not rows:
                    raise Error("No completed report exists to publish.")
                run_id = rows[0]["id"]
            with state.lock("publication", wait_seconds=120):
                publication = publisher.publish_run(state, run_id)
            print(f"Published proposal #{publication['issue_number']}: {publication['issue_url']}")
        case "inbox":
            processed = discussion.sync_once(state, args.name, max_threads=args.max_threads)
            if not processed:
                print("Inbox checked; no discussion turn was needed.")
        case "implementations":
            rows = state.rows(
                "SELECT maintainer,repository,issue_number,status,branch,pr_number,pr_url,updated_at "
                "FROM implementations ORDER BY updated_at DESC"
            )
            for item in rows:
                print(
                    f"{item['maintainer']}  {item['repository']}#{item['issue_number']}  "
                    f"{item['status']:8s}  PR #{item['pr_number']}  {item['pr_url']}"
                )
            if not rows:
                print("No implementation claims yet.")
        case "memory":
            state.one("maintainers", args.name)
            if args.action == "list":
                print(json.dumps(state.memories(args.name), indent=2, ensure_ascii=False))
            else:
                with state.lock(), state.db:
                    if args.action == "add":
                        if not args.text.strip() or len(args.text) > 4000:
                            raise Error("A memory note must contain 1-4000 characters.")
                        state.db.execute("INSERT INTO notes(maintainer,body,source,created_at) VALUES (?,?,?,?)",
                                         (args.name, args.text, "human", utcnow()))
                        print("Human guidance saved.")
                    else:
                        cursor = state.db.execute("DELETE FROM notes WHERE id=? AND maintainer=?", (args.id, args.name))
                        if not cursor.rowcount:
                            raise Error("No matching memory note.")
                        print("Memory note removed.")
        case "pause":
            (state.home / "PAUSED").touch(mode=0o600)
            print("New runs paused. An active run continues; Ctrl+C stops a foreground run.")
        case "resume":
            (state.home / "PAUSED").unlink(missing_ok=True)
            print("New runs permitted. No process or schedule was started.")
        case "serve":
            interval = _serve_interval_seconds(args.every_hours, args.every_minutes)
            serve(state, args.names, interval, args.poll_seconds)
        case "_serve-worker":
            _serve_worker_loop(state, args.name, args.interval_seconds, args.poll_seconds)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    os.umask(0o077)
    state = None
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if not sys.platform.startswith("linux"):
            raise Error("maintainerd currently supports Linux only.")
        state = State(args.home)
        return dispatch(args, state)
    except KeyboardInterrupt:
        print("Stopped. Any active Codex process group was terminated.", file=sys.stderr)
        return 130
    except (Error, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
        if state:
            state.close()
