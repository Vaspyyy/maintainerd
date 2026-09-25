"""Small command surface; no web server, hosted API, or background installation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, codex, cycle, repo
from .state import Error, State, default_home, name, utcnow


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="maintainerd", description="Independent maintainer experiments. Milestone 0 is read-only.")
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
    serve = commands.add_parser("serve", help="Opt-in foreground exploration loop; no GitHub event handling yet")
    serve.add_argument("name")
    serve.add_argument("--every-hours", type=float, default=12)
    return root


def doctor(state: State) -> int:
    failed = False
    print(f"Data: {state.home}\nPython: {sys.version.split()[0]}\nMode: read-only, ChatGPT subscription only")
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
    print(f"Remaining run starts today (UTC): {state.remaining()}/{state.config.max_runs_per_day}")
    print("Doctor does not call a model or prove sandbox enforcement. The first wake is the live integration check.")
    print("Use trusted repositories only; the Codex sandbox is not a separate VM or protection from reading your home.")
    return int(failed)


def serve(state: State, maintainer: str, hours: float) -> None:
    if not 1 <= hours <= 168:
        raise Error("--every-hours must be between 1 and 168.")
    state.one("maintainers", maintainer)
    print(f"Foreground loop for {maintainer}, every {hours:g} hours. Ctrl+C stops it.", flush=True)
    while True:
        if (state.home / "PAUSED").exists():
            time.sleep(30)
            continue
        rows = state.rows("SELECT started_at FROM runs WHERE maintainer=? AND invoked=1 "
                          "ORDER BY started_at DESC, rowid DESC LIMIT 1", (maintainer,))
        now = datetime.now(timezone.utc)
        due = datetime.fromisoformat(rows[0]["started_at"]) + timedelta(hours=hours) if rows else now
        if not state.remaining():
            due = max(due, (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        delay = (due - now).total_seconds()
        if delay > 0:
            time.sleep(min(delay, 30))
            continue
        # Any runtime error stops the loop instead of burning allowance retrying.
        cycle.wake(state, maintainer, "scheduled")


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
            serve(state, args.name, args.every_hours)
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
            raise Error("Milestone 0 currently supports Linux only.")
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
