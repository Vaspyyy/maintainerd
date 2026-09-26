"""Trusted host-side GitHub publishing and coordination.

Codex never receives GitHub credentials or a GitHub write tool.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import report
from .state import Config, Error, State, utcnow, write_json


API = "https://api.github.com"
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_./:-]{2,}")
PATH = re.compile(r"(?:src|tests|docs|examples)/[A-Za-z0-9_./-]+")
CALL_SYMBOL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
BACKTICK_SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})(?:\([^\`]*\))?`")
_SESSION_CACHE: dict[tuple[object, ...], tuple[float, "Session"]] = {}

OVERLAP_THRESHOLD = 0.76

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when", "then",
    "should", "could", "would", "existing", "current", "using", "used", "use",
    "issue", "proposal", "problem", "possible", "direction", "behavior", "change",
    "changes", "test", "tests", "file", "files", "code", "also", "only", "before",
    "after", "while", "where", "there", "their", "they", "them", "each",
}


class GithubError(Error):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Session:
    repository: str
    token: str
    bot_login: str
    permissions: dict[str, str]


def configured(config: Config) -> bool:
    return config.github_app_id is not None and config.github_private_key_path is not None


def config_for(state: State, maintainer: str) -> Config:
    rows = state.rows(
        "SELECT github_app_id,github_private_key_path FROM maintainer_identities WHERE maintainer=?",
        (maintainer,),
    )
    if not rows:
        return state.config
    row = rows[0]
    return replace(
        state.config,
        github_app_id=row["github_app_id"],
        github_private_key_path=row["github_private_key_path"],
    )


def configured_for(state: State, maintainer: str) -> bool:
    return configured(config_for(state, maintainer))


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
        raise GithubError(f"GitHub API {method} {path} failed ({message}).", exc.code) from exc
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


def _installation_access(config: Config, repository: str, app_jwt: str | None = None) -> dict:
    repository = _repository_path(repository)
    app_jwt = app_jwt or _app_jwt(config)
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


def session(config: Config, repository: str) -> Session:
    repository = _repository_path(repository)
    cache_key = (config.github_app_id, config.github_private_key_path, repository)
    cached = _SESSION_CACHE.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    app_jwt = _app_jwt(config)
    app = _api("GET", "/app", app_jwt)
    if not isinstance(app, dict) or not isinstance(app.get("slug"), str):
        raise Error("GitHub App identity could not be resolved.")
    access = _installation_access(config, repository, app_jwt)
    active = Session(
        repository,
        access["token"],
        f"{app['slug']}[bot]",
        dict(access.get("permissions") or {}),
    )
    # Installation tokens normally last an hour. Reauthenticate early rather
    # than persisting them or relying on exact remote expiry parsing.
    _SESSION_CACHE[cache_key] = (time.monotonic() + 45 * 60, active)
    return active


def preflight(config: Config, repository: str) -> str:
    active = session(config, repository)
    extras = []
    if active.permissions.get("contents") == "write":
        extras.append("contents")
    if active.permissions.get("pull_requests") == "write":
        extras.append("pull requests")
    suffix = f"; implementation permissions: {', '.join(extras)}" if extras else ""
    return (
        f"GitHub App {config.github_app_id} ({active.bot_login}) can write issue discussions "
        f"in {repository}{suffix}"
    )


def session_for(state: State, maintainer: str, repository: str) -> Session:
    return session(config_for(state, maintainer), repository)


def preflight_for(state: State, maintainer: str, repository: str) -> str:
    return preflight(config_for(state, maintainer), repository)


def require_implementation_permissions(active: Session) -> None:
    missing = [
        label
        for key, label in (("contents", "Contents: Read and write"),
                           ("pull_requests", "Pull requests: Read and write"))
        if active.permissions.get(key) != "write"
    ]
    if missing:
        raise Error("GitHub App is not implementation-ready; grant " + " and ".join(missing) + ".")


def get_ref(active: Session, branch: str) -> dict | None:
    encoded = quote(branch, safe="")
    try:
        value = _api("GET", f"/repos/{active.repository}/git/ref/heads/{encoded}", active.token)
    except GithubError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(value, dict):
        raise Error("GitHub returned an invalid branch reference.")
    return value


def get_git_commit(active: Session, sha: str) -> dict:
    value = _api("GET", f"/repos/{active.repository}/git/commits/{sha}", active.token)
    if not isinstance(value, dict):
        raise Error("GitHub returned an invalid Git commit.")
    return value


def create_empty_commit(active: Session, base_sha: str, message: str) -> dict:
    base = get_git_commit(active, base_sha)
    tree = base.get("tree") or {}
    tree_sha = tree.get("sha")
    if not isinstance(tree_sha, str):
        raise Error("Base commit did not expose a tree SHA.")
    value = _api(
        "POST",
        f"/repos/{active.repository}/git/commits",
        active.token,
        {"message": message, "tree": tree_sha, "parents": [base_sha]},
    )
    if not isinstance(value, dict) or not isinstance(value.get("sha"), str):
        raise Error("GitHub did not return the empty claim commit.")
    return value


def create_ref(active: Session, branch: str, sha: str) -> dict:
    value = _api(
        "POST",
        f"/repos/{active.repository}/git/refs",
        active.token,
        {"ref": f"refs/heads/{branch}", "sha": sha},
    )
    if not isinstance(value, dict):
        raise Error("GitHub did not return the created branch reference.")
    return value


def pulls_for_head(active: Session, branch: str) -> list[dict]:
    owner = active.repository.split("/", 1)[0]
    query = urlencode({"state": "all", "head": f"{owner}:{branch}", "per_page": 20})
    value = _api("GET", f"/repos/{active.repository}/pulls?{query}", active.token)
    if not isinstance(value, list):
        raise Error("GitHub returned an invalid pull-request list.")
    return [item for item in value if isinstance(item, dict)]


def create_draft_pr(active: Session, *, branch: str, base: str, title: str, body: str) -> dict:
    value = _api(
        "POST",
        f"/repos/{active.repository}/pulls",
        active.token,
        {
            "title": title,
            "head": branch,
            "base": base,
            "body": body,
            "draft": True,
            "maintainer_can_modify": True,
        },
    )
    if not isinstance(value, dict) or type(value.get("number")) is not int:
        raise Error("GitHub did not return the created draft pull request.")
    return value


def pull_request(active: Session, number: int) -> dict:
    value = _api("GET", f"/repos/{active.repository}/pulls/{number}", active.token)
    if not isinstance(value, dict):
        raise Error("GitHub returned an invalid pull request.")
    return value


def update_pull_request(
    active: Session,
    number: int,
    *,
    title: str | None = None,
    body: str | None = None,
) -> dict:
    payload = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if not payload:
        raise Error("Pull-request update needs a title or body.")
    value = _api(
        "PATCH",
        f"/repos/{active.repository}/pulls/{number}",
        active.token,
        payload,
    )
    if not isinstance(value, dict):
        raise Error("GitHub returned an invalid pull-request update.")
    return value


def mark_ready_for_review(active: Session, number: int) -> dict:
    pr = pull_request(active, number)
    if pr.get("state") != "open":
        raise Error("Only an open pull request can be marked ready for review.")
    if not pr.get("draft", False):
        return pr
    node_id = pr.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise Error("GitHub pull request did not expose a GraphQL node ID.")
    value = _api(
        "POST",
        "/graphql",
        active.token,
        {
            "query": (
                "mutation($id: ID!) { "
                "markPullRequestReadyForReview(input: {pullRequestId: $id}) { "
                "pullRequest { number isDraft url } "
                "} }"
            ),
            "variables": {"id": node_id},
        },
    )
    if not isinstance(value, dict):
        raise Error("GitHub returned an invalid ready-for-review response.")
    errors = value.get("errors")
    if errors:
        raise Error("GitHub refused to mark the pull request ready for review.")
    ready = (((value.get("data") or {}).get("markPullRequestReadyForReview") or {}).get("pullRequest") or {})
    if ready.get("isDraft") is not False:
        raise Error("GitHub did not confirm that the pull request is ready for review.")
    return ready


def convert_to_draft(active: Session, number: int) -> dict:
    pr = pull_request(active, number)
    if pr.get("state") != "open":
        raise Error("Only an open pull request can be converted to draft.")
    if pr.get("draft", False):
        return pr
    node_id = pr.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise Error("GitHub pull request did not expose a GraphQL node ID.")
    value = _api(
        "POST",
        "/graphql",
        active.token,
        {
            "query": (
                "mutation($id: ID!) { "
                "convertPullRequestToDraft(input: {pullRequestId: $id}) { "
                "pullRequest { number isDraft url } "
                "} }"
            ),
            "variables": {"id": node_id},
        },
    )
    if not isinstance(value, dict) or value.get("errors"):
        raise Error("GitHub refused to convert the pull request to draft.")
    draft = (((value.get("data") or {}).get("convertPullRequestToDraft") or {}).get("pullRequest") or {})
    if draft.get("isDraft") is not True:
        raise Error("GitHub did not confirm that the pull request is draft.")
    return draft


def pull_reviews(active: Session, number: int) -> list[dict]:
    value = _api(
        "GET",
        f"/repos/{active.repository}/pulls/{number}/reviews?per_page=100",
        active.token,
    )
    if not isinstance(value, list):
        raise Error("GitHub returned an invalid pull-request review list.")
    return [item for item in value if isinstance(item, dict)]


def pull_review_comments(active: Session, number: int) -> list[dict]:
    value = _api(
        "GET",
        f"/repos/{active.repository}/pulls/{number}/comments?per_page=100",
        active.token,
    )
    if not isinstance(value, list):
        raise Error("GitHub returned an invalid pull-request review-comment list.")
    return [item for item in value if isinstance(item, dict)]


def pull_files(active: Session, number: int) -> list[dict]:
    value = _api(
        "GET",
        f"/repos/{active.repository}/pulls/{number}/files?per_page=100",
        active.token,
    )
    if not isinstance(value, list):
        raise Error("GitHub returned an invalid pull-request file list.")
    return [item for item in value if isinstance(item, dict)]


def submit_review(active: Session, number: int, event: str, body: str) -> dict:
    if event not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
        raise Error("Unsupported pull-request review event.")
    if not body.strip() and event != "APPROVE":
        raise Error("A non-approval pull-request review needs a body.")
    value = _api(
        "POST",
        f"/repos/{active.repository}/pulls/{number}/reviews",
        active.token,
        {"event": event, "body": body.strip()},
    )
    if not isinstance(value, dict) or type(value.get("id")) is not int:
        raise Error("GitHub review creation returned incomplete metadata.")
    return value


def issue_thread(active: Session, issue_number: int) -> dict:
    if type(issue_number) is not int or issue_number <= 0:
        raise Error("Issue number must be a positive integer.")
    issue = _api("GET", f"/repos/{active.repository}/issues/{issue_number}", active.token)
    comments = _api(
        "GET",
        f"/repos/{active.repository}/issues/{issue_number}/comments?per_page=100",
        active.token,
    )
    if not isinstance(issue, dict) or not isinstance(comments, list):
        raise Error("GitHub returned an invalid issue thread.")
    return {"issue": issue, "comments": comments}


def post_comment(active: Session, issue_number: int, body: str) -> dict:
    if not body.strip():
        raise Error("Refusing to post an empty GitHub comment.")
    created = _api(
        "POST",
        f"/repos/{active.repository}/issues/{issue_number}/comments",
        active.token,
        {"body": body},
    )
    if not isinstance(created, dict) or type(created.get("id")) is not int:
        raise Error("GitHub comment creation returned incomplete metadata.")
    return created


def _clip(value: str, limit: int = 6000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 30].rstrip() + "\n\n[truncated by maintainerd]"


def _coordination_only_evidence(value: str) -> bool:
    text = value.casefold()
    mentions_snapshot = "supplied" in text or "snapshot" in text or "open item" in text
    dedupe_language = (
        "no matching" in text
        or "no supplied" in text
        or "contain no matching" in text
        or "contains no matching" in text
        or "no supplied item overlaps" in text
        or "recent closed" in text
    )
    return mentions_snapshot and dedupe_language and "#" in value


def _public_evidence(finding: dict) -> list[str]:
    return [
        item for item in finding["evidence"]
        if isinstance(item, str) and item.strip() and not _coordination_only_evidence(item)
    ]


def issue_body(maintainer: str, run_id: str, sha: str, finding: dict) -> str:
    marker = f"<!-- maintainerd run={run_id} finding=0 -->"
    lines = [
        marker,
        f"_Autonomously proposed by maintainerd contributor **{maintainer}** after inspecting commit `{sha}`._",
        "",
        "No implementation has been started. This issue is a proposal for maintainer discussion.",
        "",
        "## Problem", "", _clip(finding["problem"]), "", "## Evidence", "",
    ]
    lines.extend(f"- {_clip(item, 1500)}" for item in _public_evidence(finding)[:12])
    lines.extend([
        "", "## Possible direction", "", _clip(finding["proposal"]),
        "", "## Tradeoffs", "", _clip(finding["tradeoffs"]),
    ])
    if finding["questions"]:
        lines.extend(["", "## Questions", ""])
        lines.extend(f"- {_clip(item, 1500)}" for item in finding["questions"][:12])
    lines.extend(["", f"_maintainerd run: `{run_id}`_"])
    return "\n".join(lines).strip() + "\n"


def overlap_comment(maintainer: str, run_id: str, sha: str, finding: dict) -> str:
    marker = f"<!-- maintainerd overlap-run={run_id} finding=0 -->"
    lines = [
        marker,
        f"**{maintainer}** independently investigated this area at `{sha}` and found overlapping evidence.",
        "",
        "Rather than open a duplicate issue, I’m adding the finding here.",
        "",
        "### Additional finding",
        "",
        _clip(finding["problem"]),
        "",
        "### Evidence",
        "",
    ]
    lines.extend(f"- {_clip(item, 1500)}" for item in _public_evidence(finding)[:12])
    lines.extend(["", "### Suggested direction", "", _clip(finding["proposal"])])
    if finding["tradeoffs"].strip():
        lines.extend(["", "### Tradeoffs", "", _clip(finding["tradeoffs"])])
    if finding["questions"]:
        lines.extend(["", "### Questions", ""])
        lines.extend(f"- {_clip(item, 1500)}" for item in finding["questions"][:12])
    return "\n".join(lines).strip() + "\n"


def _terms(text: str) -> set[str]:
    return {
        token.casefold().strip("./:-")
        for token in WORD.findall(text)
        if token.casefold().strip("./:-") not in STOPWORDS
    }


def _paths(text: str) -> set[str]:
    return {match.rstrip(".,:;)").casefold() for match in PATH.findall(text)}


_GENERIC_SYMBOLS = {
    "save", "preview", "load", "validate", "update", "create", "delete",
    "serialize", "render", "write", "read", "get", "set",
}


def _symbols(text: str) -> set[str]:
    values = {match.casefold() for match in CALL_SYMBOL.findall(text)}
    values.update(match.casefold() for match in BACKTICK_SYMBOL.findall(text))
    return {value for value in values if value not in _GENERIC_SYMBOLS}


def similarity(finding: dict, item: dict) -> float:
    candidate_title = " ".join(finding["title"].casefold().split())
    existing_title = " ".join(str(item.get("title") or "").casefold().split())
    title_ratio = SequenceMatcher(None, candidate_title, existing_title).ratio()
    candidate_text = "\n".join([
        finding["title"], finding["problem"], *finding["evidence"], finding["proposal"],
    ])
    existing_text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
    left, right = _terms(candidate_text), _terms(existing_text)
    overlap = len(left & right) / max(1, min(len(left), len(right)))
    paths_left, paths_right = _paths(candidate_text), _paths(existing_text)
    path_score = (
        len(paths_left & paths_right) / max(1, min(len(paths_left), len(paths_right)))
        if paths_left and paths_right else 0
    )
    symbols_left, symbols_right = _symbols(candidate_text), _symbols(existing_text)
    shared_symbols = symbols_left & symbols_right
    symbol_score = (
        len(shared_symbols) / max(1, min(len(symbols_left), len(symbols_right)))
        if symbols_left and symbols_right else 0
    )
    composite = (
        0.32 * title_ratio
        + 0.23 * overlap
        + 0.15 * path_score
        + 0.30 * symbol_score
    )
    # Two reports naming the same concrete code path and multiple same symbols
    # are almost certainly the same engineering thread even when models title it differently.
    if path_score >= 0.5 and len(shared_symbols) >= 2:
        composite = max(composite, 0.82)
    return max(title_ratio, composite)


def _record_route(
    state: State,
    run_id: str,
    repository: str,
    item: dict,
    title: str,
    mode: str,
    comment_id: int | None = None,
) -> dict:
    number = item.get("number")
    url = item.get("html_url")
    if type(number) is not int or not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise Error("GitHub publication returned incomplete issue metadata.")
    with state.db:
        state.db.execute(
            "INSERT OR IGNORE INTO proposal_routes"
            "(run_id,finding_index,repository,issue_number,issue_url,title,mode,comment_id,published_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, 0, repository, number, url, title, mode, comment_id, utcnow()),
        )
    rows = state.rows("SELECT * FROM proposal_routes WHERE run_id=? AND finding_index=0", (run_id,))
    if not rows:
        raise Error("Proposal route could not be recorded locally.")
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
        "SELECT * FROM proposal_routes WHERE run_id=? ORDER BY finding_index", (run["id"],)
    )
    parsed = json.loads(run["result"])
    (artifacts / "report.md").write_text(
        report.markdown(parsed, run["id"], run.get("commit_sha") or "", limitations, publications),
        encoding="utf-8",
    )
    write_json(artifacts / "publication.json", publications)


def _existing_owner(state: State, repository: str, issue_number: int) -> set[str]:
    rows = state.rows(
        "SELECT DISTINCT r.maintainer FROM proposal_routes p "
        "JOIN runs r ON r.id=p.run_id WHERE p.repository=? AND p.issue_number=?",
        (repository, issue_number),
    )
    return {row["maintainer"] for row in rows}


def publish_run(state: State, run_id: str) -> dict:
    run = state.one("runs", run_id)
    if run["status"] != "completed" or not run.get("result"):
        raise Error("Only a completed, validated maintenance run can be published.")
    parsed = json.loads(run["result"])
    if parsed.get("outcome") != "propose" or len(parsed.get("findings", [])) != 1:
        raise Error("This run does not contain exactly one publishable proposal.")

    existing = state.rows("SELECT * FROM proposal_routes WHERE run_id=? AND finding_index=0", (run_id,))
    if existing:
        return existing[0]

    maintainer = state.one("maintainers", run["maintainer"])
    repository = state.one("repositories", maintainer["repository"])
    target = repository.get("github")
    if not target:
        raise Error("The managed repository has no GitHub owner/repo configured.")

    active = session_for(state, maintainer["name"], target)
    finding = parsed["findings"][0]
    title = finding["title"].strip()
    marker = f"<!-- maintainerd run={run_id} finding=0 -->"

    recent = _api(
        "GET",
        f"/repos/{_repository_path(target)}/issues?state=all&sort=updated&direction=desc&per_page=100",
        active.token,
    )
    if not isinstance(recent, list):
        raise Error("GitHub issues response was not a list.")

    ranked: list[tuple[float, dict]] = []
    for item in recent:
        if not isinstance(item, dict):
            continue
        body = item.get("body") or ""
        if marker in body:
            route = _record_route(state, run_id, target, item, title, "created")
            _refresh_report(state, run)
            return route
        ranked.append((similarity(finding, item), item))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if ranked and ranked[0][0] >= OVERLAP_THRESHOLD:
        score, item = ranked[0]
        number = item.get("number")
        if type(number) is not int:
            raise Error("Potential duplicate did not have a valid issue number.")
        if item.get("state") != "open":
            raise Error(
                f"Proposal strongly overlaps closed GitHub item #{number} (similarity {score:.2f}). "
                "Review that history before reopening the topic."
            )
        owners = _existing_owner(state, target, number)
        if maintainer["name"] in owners:
            raise Error(
                f"This maintainer already has an overlapping open thread at #{number} "
                f"(similarity {score:.2f}); no duplicate comment was posted."
            )
        overlap_marker = f"<!-- maintainerd overlap-run={run_id} finding=0 -->"
        thread = issue_thread(active, number)
        for comment in thread["comments"]:
            if overlap_marker in (comment.get("body") or ""):
                route = _record_route(
                    state, run_id, target, item, title, "joined", comment.get("id")
                )
                _refresh_report(state, run)
                return route
        created_comment = post_comment(
            active,
            number,
            overlap_comment(maintainer["name"], run_id, run.get("commit_sha") or "", finding),
        )
        route = _record_route(
            state, run_id, target, item, title, "joined", created_comment.get("id")
        )
        _refresh_report(state, run)
        return route

    created = _api(
        "POST",
        f"/repos/{_repository_path(target)}/issues",
        active.token,
        {"title": title, "body": issue_body(maintainer["name"], run_id, run.get("commit_sha") or "", finding)},
    )
    if not isinstance(created, dict):
        raise Error("GitHub issue creation returned an invalid response.")
    route = _record_route(state, run_id, target, created, title, "created")
    _refresh_report(state, run)
    return route
