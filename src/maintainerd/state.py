"""Local configuration, durable state, and a single-host process lock."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class Error(Exception):
    """An actionable error suitable for the command line."""


class Busy(Error):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def name(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise Error("Names must contain 1-64 letters, numbers, underscores or hyphens.")
    return value


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


DEFAULT_CONFIG = '''# No API keys. Codex must be logged in with ChatGPT.
codex_binary = "codex"
timeout_seconds = 900
max_runs_per_day = 4
include_github = true
publish_proposals = false
# GitHub App identity used only by the trusted host-side proposal publisher.
# github_app_id = 123456
# github_private_key_path = "/home/you/.config/maintainerd/mira.private-key.pem"
# Optional: use a model available in your Codex subscription.
# model = "your-model-id"
'''


@dataclass(frozen=True)
class Config:
    codex_binary: str = "codex"
    timeout_seconds: int = 900
    max_runs_per_day: int = 4
    include_github: bool = True
    publish_proposals: bool = False
    github_app_id: int | None = None
    github_private_key_path: str | None = None
    model: str | None = None

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise Error(f"Cannot read configuration {path}: {exc}") from exc
        unknown = data.keys() - {field.name for field in fields(cls)}
        if unknown:
            raise Error(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        config = cls(**data)
        for key, maximum in (("timeout_seconds", 7200), ("max_runs_per_day", 100)):
            value = getattr(config, key)
            if type(value) is not int or not 1 <= value <= maximum:
                raise Error(f"{key} must be an integer between 1 and {maximum}.")
        if type(config.include_github) is not bool:
            raise Error("include_github must be true or false.")
        if type(config.publish_proposals) is not bool:
            raise Error("publish_proposals must be true or false.")
        if config.github_app_id is not None and (
            type(config.github_app_id) is not int or config.github_app_id <= 0
        ):
            raise Error("github_app_id must be a positive integer.")
        if config.github_private_key_path is not None and (
            not isinstance(config.github_private_key_path, str)
            or not config.github_private_key_path.strip()
            or "\n" in config.github_private_key_path
        ):
            raise Error("github_private_key_path must be a nonempty path string.")
        if (config.github_app_id is None) != (config.github_private_key_path is None):
            raise Error("Configure github_app_id and github_private_key_path together.")
        if config.publish_proposals and config.github_app_id is None:
            raise Error("publish_proposals requires a configured GitHub App identity.")
        for key in ("codex_binary", "model"):
            value = getattr(config, key)
            if value is None and key == "model":
                continue
            if not isinstance(value, str) or not value.strip() or "\n" in value:
                raise Error(f"{key} must be a nonempty string.")
        return config


class State:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        if self.home in (Path("/"), Path.home().resolve()):
            raise Error("Use a dedicated maintainerd data directory, not your home or filesystem root.")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        for subdirectory in ("repos", "runs", "threads", "workspaces"):
            (self.home / subdirectory).mkdir(exist_ok=True, mode=0o700)
        config_path = self.home / "config.toml"
        try:
            with config_path.open("x", encoding="utf-8") as file:
                file.write(DEFAULT_CONFIG)
        except FileExistsError:
            pass
        self.config = Config.load(config_path)
        self.db = sqlite3.connect(self.home / "state.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            self.db.close()
            raise Error(f"State schema {version} is newer than this maintainerd supports.")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS repositories (
                name TEXT PRIMARY KEY, source TEXT NOT NULL,
                branch TEXT NOT NULL, github TEXT
            );
            CREATE TABLE IF NOT EXISTS maintainers (
                name TEXT PRIMARY KEY, repository TEXT NOT NULL REFERENCES repositories(name),
                mission TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, maintainer TEXT NOT NULL REFERENCES maintainers(name),
                reason TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, commit_sha TEXT, result TEXT, error TEXT,
                usage TEXT, invoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY, maintainer TEXT NOT NULL REFERENCES maintainers(name),
                body TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposal_publications (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                finding_index INTEGER NOT NULL,
                repository TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                issue_url TEXT NOT NULL,
                title TEXT NOT NULL,
                published_at TEXT NOT NULL,
                UNIQUE(run_id, finding_index),
                UNIQUE(repository, issue_number)
            );
            CREATE TABLE IF NOT EXISTS proposal_routes (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                finding_index INTEGER NOT NULL,
                repository TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                issue_url TEXT NOT NULL,
                title TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('created','joined')),
                comment_id INTEGER,
                published_at TEXT NOT NULL,
                UNIQUE(run_id, finding_index)
            );
            CREATE TABLE IF NOT EXISTS maintainer_identities (
                maintainer TEXT PRIMARY KEY REFERENCES maintainers(name) ON DELETE CASCADE,
                github_app_id INTEGER NOT NULL,
                github_private_key_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thread_events (
                id INTEGER PRIMARY KEY,
                maintainer TEXT NOT NULL REFERENCES maintainers(name),
                repository TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                comment_id INTEGER NOT NULL,
                author TEXT NOT NULL,
                author_type TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','processed','ignored')),
                processed_at TEXT,
                turn_id TEXT,
                UNIQUE(maintainer, repository, comment_id)
            );
            CREATE TABLE IF NOT EXISTS thread_turns (
                id TEXT PRIMARY KEY,
                maintainer TEXT NOT NULL REFERENCES maintainers(name),
                repository TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                commit_sha TEXT,
                trigger_comment_ids TEXT NOT NULL,
                result TEXT,
                error TEXT,
                usage TEXT,
                invoked INTEGER NOT NULL DEFAULT 0,
                reply_comment_id INTEGER,
                reply_url TEXT
            );
            INSERT OR IGNORE INTO proposal_routes(
                run_id,finding_index,repository,issue_number,issue_url,title,mode,comment_id,published_at
            )
            SELECT run_id,finding_index,repository,issue_number,issue_url,title,'created',NULL,published_at
            FROM proposal_publications;
            PRAGMA user_version=4;
        ''')

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def lock(self) -> Iterator[int]:
        with (self.home / "lock").open("a+") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy("Another maintainerd operation is running. Let it finish or stop it first.") from exc
            try:
                yield file.fileno()
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def one(self, table: str, key: str) -> dict:
        if table not in ("repositories", "maintainers", "runs"):
            raise ValueError("Unsupported table")
        column = "id" if table == "runs" else "name"
        row = self.db.execute(f"SELECT * FROM {table} WHERE {column}=?", (key,)).fetchone()
        if not row:
            raise Error(f"Unknown {table.rstrip('s')}: {key}")
        return dict(row)

    def rows(self, query: str, parameters: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.db.execute(query, parameters)]

    def recent(self, maintainer: str) -> list[dict]:
        rows = self.rows(
            "SELECT id, started_at, commit_sha, result FROM runs "
            "WHERE maintainer=? AND status='completed' ORDER BY started_at DESC, rowid DESC LIMIT 5",
            (maintainer,),
        )
        for row in rows:
            row["result"] = json.loads(row["result"])
        return rows

    def memories(self, maintainer: str) -> dict:
        result = {}
        for label, predicate in (("human_notes", "source='human'"), ("agent_observations", "source!='human'")):
            result[label] = self.rows(
                f"SELECT id, body, source, created_at FROM notes WHERE maintainer=? AND {predicate} "
                "ORDER BY id DESC LIMIT 20", (maintainer,),
            )
        return result

    def remaining(self) -> int:
        used_runs = self.db.execute(
            "SELECT count(*) FROM runs WHERE invoked=1 AND substr(started_at,1,10)=?",
            (utcnow()[:10],),
        ).fetchone()[0]
        used_threads = self.db.execute(
            "SELECT count(*) FROM thread_turns WHERE invoked=1 AND substr(started_at,1,10)=?",
            (utcnow()[:10],),
        ).fetchone()[0]
        return max(0, self.config.max_runs_per_day - used_runs - used_threads)


def default_home() -> Path:
    if os.environ.get("MAINTAINERD_HOME"):
        return Path(os.environ["MAINTAINERD_HOME"])
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "maintainerd"
