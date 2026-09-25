"""Controller-owned Git copies. Never operate in the user's source checkout."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from .state import Error, State, name


def command(args: list[str], *, cwd: Path | None = None, env: dict | None = None,
            timeout: int = 90) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(f"{Path(args[0]).name} could not finish: {exc}") from exc
    if result.returncode:
        # Do not echo subprocess output: credential helpers can print secrets.
        raise Error(f"{Path(args[0]).name} failed (exit {result.returncode}). "
                    "Check authentication, the repository URL/ref, and tool installation.")
    return result.stdout


def git(args: list[str], *, cwd: Path | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_LFS_SKIP_SMUDGE="1")
    return command(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                    "-c", "core.quotePath=false", *args], cwd=cwd, env=env)


def source_info(source: str) -> tuple[str, str | None]:
    if re.fullmatch(r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", source):
        return source, source.split(":", 1)[1].removesuffix(".git")
    parsed = urlparse(source)
    if parsed.scheme:
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise Error("Use a credential-free HTTPS URL, git@github.com:owner/repo.git, or a local path.")
        github = parsed.path.strip("/").removesuffix(".git") if parsed.hostname == "github.com" else None
        if github and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github):
            raise Error("Use a repository URL, not an issue, file or branch URL.")
        return source, github
    path = Path(source).expanduser().resolve()
    if not path.is_dir():
        raise Error(f"Local repository does not exist: {path}")
    return str(path), None


def add(state: State, source: str, alias: str | None, branch: str | None,
        github: str | None = None) -> str:
    source, detected = source_info(source)
    alias = name(alias or source.rstrip("/").split("/")[-1].removesuffix(".git"))
    github = github or detected
    if github and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github):
        raise Error("--github must be owner/repository.")
    existing = state.rows("SELECT * FROM repositories WHERE name=?", (alias,))
    if existing:
        item = existing[0]
        if item["source"] == source and item["github"] == github and (branch is None or branch == item["branch"]):
            return alias
        raise Error(f"Repository name {alias} is already registered with different settings.")
    bare = state.home / "repos" / f"{alias}.git"
    if bare.exists():
        raise Error(f"Unregistered checkout exists at {bare}; inspect it before removing it manually.")
    try:
        git(["clone", "--bare", "--no-hardlinks", "--", source, str(bare)])
        if branch is None:
            branch = git(["--git-dir", str(bare), "symbolic-ref", "--short", "HEAD"]).strip()
        git(["check-ref-format", f"refs/heads/{branch}"])
        git(["--git-dir", str(bare), "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"])
        # Defense in depth. This program implements no push operation.
        git(["--git-dir", str(bare), "config", "remote.origin.pushurl", "disabled://maintainerd-read-only"])
        with state.db:
            state.db.execute("INSERT INTO repositories VALUES (?,?,?,?)", (alias, source, branch, github))
    except BaseException:
        shutil.rmtree(bare, ignore_errors=True)
        raise
    return alias


def prepare(state: State, repository: dict, run_id: str) -> tuple[Path, str, str]:
    bare = state.home / "repos" / f"{repository['name']}.git"
    branch = repository["branch"]
    git(["--git-dir", str(bare), "fetch", "--no-tags", "origin",
         f"+refs/heads/{branch}:refs/heads/{branch}"])
    sha = git(["--git-dir", str(bare), "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"] ).strip()
    workspace = state.home / "workspaces" / run_id
    git(["--git-dir", str(bare), "worktree", "add", "--detach", str(workspace), sha])
    history = git(["log", "-20", "--format=%h %aI %s"], cwd=workspace)
    return workspace, sha, history


def unchanged(workspace: Path, sha: str) -> bool:
    return (git(["rev-parse", "HEAD"], cwd=workspace).strip() == sha
            and not git(["status", "--porcelain", "--untracked-files=all", "--ignored"], cwd=workspace).strip())


def cleanup(state: State, repository: dict, workspace: Path) -> None:
    bare = state.home / "repos" / f"{repository['name']}.git"
    git(["--git-dir", str(bare), "worktree", "remove", str(workspace)])
