"""Trusted host-side GitHub proposal publisher.

The Codex process never receives these credentials or a GitHub write tool.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import report
from .state import Config, Error, State, utcnow, write_json


API = "https://api.github.com"
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def configured(config: Config) -> bool:
    return config.github_app_id is not None and config.github_private_key_path is not None


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _private_key(config: Config) -> Path:
    if not configured(config):
        raise Error("GitHub App publishing is not configured.")
    path = Path(config.github_private_key_path or "").expanduser().resolve()
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise Error(f"Cannot read GitHub App private key at {path}.") from exc
    if not path.is_file():
        raise Error("GitHub App private key path is not a regular file.")
    if mode & 0o077:
        raise Error(f"GitHub App private key is too broadly readable. Run: chmod 600 {path}")
    if not shutil.which("openssl"):
        raise Error("openssl is required to sign GitHub App authentication JWTs.")
    return path


def _app_jwt(config: Config) -> str:
    key = _private_key(config)
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(
        {"iat": now - 60, "exp": now + 540, "iss": config.github_app_id},
        separators=(",", ":"),
    ).encode())
    unsigned = f"{header}.{payload}"
    try:
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key)],
            input=unsigned.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error("Could not invoke openssl for GitHub App authentication.") from exc
    if signed.returncode != 0 or not signed.stdout:
        raise Error("GitHub App private key could not sign an authentication JWT.")
    return f"{unsigned}.{_b64url(signed.stdout)}"


def _api(method: str, path: str, authorization: str, payload: dict | None = None) -> object:
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {authorization}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "maintainerd",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        message = f"HTTP {exc.code}"
        try:
            parsed = json.loads(exc.read().decode("utf-8", errors="replace"))
            if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
                message += f": {parsed['message'][:300]}"
        except Exception:
            pass
        raise Error(f"GitHub API {method} {path} failed ({message}).") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise Error(f"GitHub API {method} {path} was unavailable.") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Error("GitHub API returned invalid JSON.") from exc


def _repository_path(repository: str) -> str:
    if not REPOSITORY.fullmatch(repository):
        raise Error("GitHub repository must be owner/name.")
    return repository


def _installation_access(config: Config, repository: str) -> dict:
    repository = _repository_path(repository)
    app_jwt = _app_jwt(config)
    installation = _api("GET", f"/repos/{repository}/installation", app_jwt)
    if not isinstance(installation, dict) or type(installation.get("id")) is not int:
        raise Error("GitHub App is not installed on the target repository.")
    access = _api("POST", f"/app/installations/{installation['id']}/access_tokens", app_jwt, {})
    if not isinstance(access, dict) or not isinstance(access.get("token"), str):
        raise Error("GitHub did not return an installation access token.")
    permissions = access.get("permissions") or {}
    if permissions.get("issues") != "write":
        raise Error("GitHub App installation needs Issues: Read and write permission.")
    return access


def preflight(config: Config, repository: str) -> str:
    _installation_access(config, repository)
    return f"GitHub App {config.github_app_id} can publish proposal issues to {repository}"


def _clip(value: str, limit: int = 6000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 30].rstrip() + "\n\n[truncated by maintainerd]"


def issue_body(maintainer: str, run_id: str, sha: str, finding: dict) -> str:
    marker = f"<!-- maintainerd run={run_id} finding=0 -->"
    lines = [
        marker,
        f"_Autonomously proposed by maintainerd contributor **{maintainer}** after inspecting commit `{sha}`._",
        "",
        "No implementation has been started. This issue is a proposal for maintainer discussion.",
        "",
        "## Problem",
        "",
        _clip(finding["problem"]),
        "",
        "## Evidence",
        "",
    ]
    lines.extend(f"- {_clip(item, 1500)}" for item in finding["evidence"][:12])
    lines.extend([
        "", "## Possible direction", "", _clip(finding["proposal"]),
        "", "## Tradeoffs", "", _clip(finding["tradeoffs"]),
    ])
    if finding["questions"]:
        lines.extend(["", "## Questions", ""])
        lines.extend(f"- {_clip(item, 1500)}" for item in finding["questions"][:12])
    lines.extend(["", f"_maintainerd run: `{run_id}`_"])
    return "\n".join(lines).strip() + "\n"


def _record(state: State, run_id: str, repository: str, issue: dict, title: str) -> dict:
    number = issue.get("number")
    url = issue.get("html_url")
    if type(number) is not int or not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise Error("GitHub issue creation returned incomplete metadata.")
    with state.db:
        state.db.execute(
            "INSERT OR IGNORE INTO proposal_publications"
            "(run_id,finding_index,repository,issue_number,issue_url,title,published_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, 0, repository, number, url, title, utcnow()),
        )
    rows = state.rows("SELECT * FROM proposal_publications WHERE run_id=? AND finding_index=0", (run_id,))
    if not rows:
        raise Error("Proposal publication could not be recorded locally.")
    return rows[0]


def _refresh_report(state: State, run: dict) -> None:
    artifacts = state.home / "runs" / run["id"]
    if not run.get("result") or not (artifacts / "context.json").is_file():
        return
    context = json.loads((artifacts / "context.json").read_text(encoding="utf-8"))
    limitations = [
        *context.get("controller_limitations", []),
        *((context.get("github") or {}).get("limitations") or []),
    ]
    publications = state.rows(
        "SELECT * FROM proposal_publications WHERE run_id=? ORDER BY finding_index", (run["id"],)
    )
    parsed = json.loads(run["result"])
    (artifacts / "report.md").write_text(
        report.markdown(parsed, run["id"], run.get("commit_sha") or "", limitations, publications),
        encoding="utf-8",
    )
    write_json(artifacts / "publication.json", publications)


def publish_run(state: State, run_id: str) -> dict:
    run = state.one("runs", run_id)
    if run["status"] != "completed" or not run.get("result"):
        raise Error("Only a completed, validated maintenance run can be published.")
    parsed = json.loads(run["result"])
    if parsed.get("outcome") != "propose" or len(parsed.get("findings", [])) != 1:
        raise Error("This run does not contain exactly one publishable proposal.")

    existing = state.rows(
        "SELECT * FROM proposal_publications WHERE run_id=? AND finding_index=0", (run_id,)
    )
    if existing:
        return existing[0]

    maintainer = state.one("maintainers", run["maintainer"])
    repository = state.one("repositories", maintainer["repository"])
    target = repository.get("github")
    if not target:
        raise Error("The managed repository has no GitHub owner/repo configured.")

    access = _installation_access(state.config, target)
    token = access["token"]
    finding = parsed["findings"][0]
    title = finding["title"].strip()
    marker = f"<!-- maintainerd run={run_id} finding=0 -->"

    recent = _api(
        "GET",
        f"/repos/{_repository_path(target)}/issues?state=all&sort=created&direction=desc&per_page=100",
        token,
    )
    if not isinstance(recent, list):
        raise Error("GitHub issues response was not a list.")
    for item in recent:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        body = item.get("body") or ""
        if marker in body:
            publication = _record(state, run_id, target, item, title)
            _refresh_report(state, run)
            return publication
        if item.get("state") == "open" and str(item.get("title") or "").casefold() == title.casefold():
            raise Error(f"An open issue already has the title {title!r}; review it before publishing a duplicate.")

    created = _api(
        "POST",
        f"/repos/{_repository_path(target)}/issues",
        token,
        {"title": title, "body": issue_body(maintainer["name"], run_id, run.get("commit_sha") or "", finding)},
    )
    if not isinstance(created, dict):
        raise Error("GitHub issue creation returned an invalid response.")
    publication = _record(state, run_id, target, created, title)
    _refresh_report(state, run)
    return publication
