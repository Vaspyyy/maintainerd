"""Subscription-only Codex CLI adapter. No API client and no billing fallback."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from .state import Config, Error, write_json


REQUIRED_EXEC_FLAGS = (
    "--json", "--sandbox", "--output-schema", "--output-last-message",
    "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
)


class RuntimeFailure(Error):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


def environment() -> dict[str, str]:
    # In particular, do not inherit API keys, GH tokens, SSH_AUTH_SOCK, arbitrary
    # provider settings, or OPENAI/CODEX workload identity environment variables.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM",
               "CODEX_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
               "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
               "SSL_CERT_FILE", "SSL_CERT_DIR"}
    result = {key: value for key, value in os.environ.items()
              if key in allowed or key.startswith("LC_")}
    result.update(NO_COLOR="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                  PYTHONDONTWRITEBYTECODE="1")
    return result


def preflight(config: Config) -> str:
    executable = shutil.which(config.codex_binary)
    if not executable:
        raise Error(f"Codex executable not found: {config.codex_binary}")
    env = environment()
    # An ordinary status check must not use forced_login_method: Codex may log
    # out mismatching credentials. Inspect first, then enforce on actual runs.
    with tempfile.TemporaryDirectory(prefix="maintainerd-check-", dir="/tmp") as directory:
        def probe(args: list[str]) -> subprocess.CompletedProcess:
            try:
                return subprocess.run([executable, *args], cwd=directory, env=env,
                                      stdin=subprocess.DEVNULL, capture_output=True,
                                      text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise Error("Codex preflight failed; check 'codex --version' and 'codex login status'.") from exc

        version = probe(["--version"])
        if version.returncode:
            raise Error("codex --version failed.")
        help_result = probe(["exec", "--help"])
        help_text = help_result.stdout + help_result.stderr
        missing = [flag for flag in REQUIRED_EXEC_FLAGS if flag not in help_text]
        if help_result.returncode or missing:
            raise Error("Codex is missing required safe-automation flags: " + ", ".join(missing)
                        + ". Update Codex; maintainerd will not fall back to a less restricted invocation.")
        global_help = probe(["--help"])
        if global_help.returncode or "--strict-config" not in global_help.stdout + global_help.stderr:
            raise Error("Update Codex to a version supporting --strict-config.")
        auth = probe(["login", "status"])
        text = (auth.stdout + auth.stderr).lower()
        if auth.returncode or "logged in using chatgpt" not in text or "api key" in text:
            raise Error("ChatGPT subscription login was not confirmed. Run 'codex login' locally. "
                        "API-key and unknown authentication modes are refused; no credentials were changed.")
        return version.stdout.strip() or "Codex available"


def argv(config: Config, control: Path, artifacts: Path) -> list[str]:
    args = [config.codex_binary, "exec", "--ignore-user-config", "--ignore-rules",
            "--ephemeral", "--strict-config", "--json", "--color", "never",
            "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(control)]
    overrides = {
        "approval_policy": "never",
        "model_provider": "openai",
        "forced_login_method": "chatgpt",
        "cli_auth_credentials_store": "auto",
        "web_search": "disabled",
        "features.apps": False,
        "features.plugins": False,
        "features.remote_plugin": False,
        "features.hooks": False,
        "features.multi_agent": False,
        "features.memories": False,
        "features.goals": False,
        "shell_environment_policy.inherit": "core",
        "shell_environment_policy.ignore_default_excludes": False,
        "shell_environment_policy.experimental_use_profile": False,
        "hide_agent_reasoning": True,
    }
    for key, value in overrides.items():
        args.extend(["-c", f"{key}={json.dumps(value)}"])
    if config.model:
        args.extend(["--model", config.model])
    args.extend(["--output-schema", str(artifacts / "schema.json"),
                 "--output-last-message", str(artifacts / "result.json"), "-"])
    return args


def stop_group(process: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
        # The parent can exit before a tool subprocess. Still kill its group.
        if sig == signal.SIGTERM:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        break
    process.wait()


def events(path: Path) -> dict:
    usage: dict[str, int] = {}
    thread_id = None
    last_turn = None
    if not path.exists():
        return {"usage": usage, "thread_id": thread_id, "last_turn": last_turn}
    with path.open(encoding="utf-8", errors="replace") as file:
        for line in file:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "thread.started":
                thread_id = event.get("thread_id")
            if kind in ("turn.completed", "turn.failed"):
                last_turn = kind
            if kind == "turn.completed" and isinstance(event.get("usage"), dict):
                for key, value in event["usage"].items():
                    if type(value) is int and value >= 0:
                        usage[key] = usage.get(key, 0) + value
    return {"usage": usage, "thread_id": thread_id, "last_turn": last_turn}


def execute(config: Config, artifacts: Path, prompt: str, lock_fd: int) -> dict:
    # Start outside the target tree so its .codex/config.toml is not loaded.
    # Personal config/rules and tool integrations are disabled; cached auth is
    # reused directly. We never copy, inspect, or serialize credential files.
    with tempfile.TemporaryDirectory(prefix="maintainerd-control-", dir="/tmp") as directory:
        control = Path(directory)
        args = argv(config, control, artifacts)
        write_json(artifacts / "invocation.json", {"argv": args, "auth": "chatgpt", "sandbox": "read-only"})
        with (artifacts / "events.jsonl").open("w", encoding="utf-8") as stdout, \
                (artifacts / "stderr.log").open("w", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen(args, cwd=control, env=environment(), stdin=subprocess.PIPE,
                                           stdout=stdout, stderr=stderr, text=True, start_new_session=True,
                                           pass_fds=(lock_fd,))
            except OSError as exc:
                raise RuntimeFailure("Could not launch Codex; inspect its configured executable.") from exc
            try:
                process.communicate(input=prompt, timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                stop_group(process)
                raise RuntimeFailure(f"Codex exceeded {config.timeout_seconds} seconds. No automatic retry.",
                                     "timed_out") from exc
            except BaseException:
                stop_group(process)
                raise
        parsed = events(artifacts / "events.jsonl")
        if process.returncode or parsed["last_turn"] == "turn.failed":
            raise RuntimeFailure(f"Codex failed (exit {process.returncode}). Inspect stderr.log/events.jsonl locally. "
                                 "Quota, auth and sandbox failures never trigger API fallback or automatic retries.")
        result_path = artifacts / "result.json"
        if not result_path.is_file() or result_path.stat().st_size > 256_000:
            raise RuntimeFailure("Codex did not produce a reasonably sized final JSON report.")
        return parsed
