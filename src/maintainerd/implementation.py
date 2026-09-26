"""Approved implementation workflow: remote claim, draft PR, then one visible commit per turn."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from . import codex, publisher, repo, report
from .state import Error, State, utcnow, write_json


APPROVAL_PATTERNS = (
    re.compile(r"^\s*/implement(?:\s|$)", re.I),
    re.compile(r"\bapproved\s+for\s+implementation\b", re.I),
    re.compile(r"\bgo\s+ahead\s+(?:and\s+)?implement\b", re.I),
    re.compile(r"\b(?:please\s+)?implement\s+(?:it|this|that|option\b)", re.I),
    re.compile(r"\bmake\s+(?:the|a)\s+(?:draft\s+)?pr\b", re.I),
    re.compile(r"\bopen\s+(?:the|a)\s+(?:draft\s+)?pr\b", re.I),
    re.compile(
        r"\b(?:you\s+)?(?:may|can)\s+(?:go\s+ahead\s+and\s+)?"
        r"(?:create|open|make)\s+(?:the|a)\s+(?:draft\s+)?pr\b",
        re.I,
    ),
    re.compile(
        r"\bfeel\s+free\s+to\s+(?:create|open|make)\s+(?:the|a)\s+"
        r"(?:draft\s+)?pr\b",
        re.I,
    ),
)

TEXT = {"type": "string", "maxLength": 12000}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 30}
STEP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["commit", "done", "needs_input"]},
        "complete": {"type": "boolean"},
        "summary": TEXT,
        "commit_message": {"type": "string", "maxLength": 120},
        "tests_run": STRINGS,
        "limitations": STRINGS,
        "memory_notes": {**STRINGS, "maxItems": 5},
    },
    "required": [
        "action", "complete", "summary", "commit_message", "tests_run", "limitations", "memory_notes"
    ],
}

FORBIDDEN_PREFIXES = (".github/", ".git/")
FORBIDDEN_PATHS = {"AGENTS.md", "CLAUDE.md", ".gitmodules"}


class ClaimLost(Error):
    """Another maintainer won the canonical remote implementation branch."""


def explicit_approval(body: str) -> bool:
    text = body.strip()
    return any(pattern.search(text) for pattern in APPROVAL_PATTERNS)


def approval_event(events: list[dict], repository: str) -> dict | None:
    owner = repository.split("/", 1)[0].casefold()
    matches = [
        event for event in events
        if str(event.get("author") or "").casefold() == owner
        and str(event.get("author_type") or "").casefold() != "bot"
        and explicit_approval(str(event.get("body") or ""))
    ]
    return matches[-1] if matches else None


def parse_step(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Error("Final Codex implementation result was not valid JSON.") from exc
    report.check(value, STEP_SCHEMA, "implementation")
    if value["action"] == "commit":
        message = value["commit_message"].strip()
        if not message or "\n" in message:
            raise Error("A commit implementation step needs a one-line commit_message.")
    elif value["commit_message"].strip():
        raise Error("done/needs_input must not invent a commit message.")
    if value["action"] == "done" and not value["complete"]:
        raise Error("done must set complete=true.")
    if value["action"] == "needs_input" and value["complete"]:
        raise Error("needs_input cannot set complete=true.")
    return value


def branch_for(issue_number: int) -> str:
    if type(issue_number) is not int or issue_number <= 0:
        raise Error("Issue number must be positive.")
    return f"maintainerd/issue-{issue_number}"


def _claim_message(issue_number: int, maintainer: str, claim_id: str) -> str:
    return (
        f"chore: claim #{issue_number} for implementation\n\n"
        f"Maintainerd-Issue: {issue_number}\n"
        f"Maintainerd-Maintainer: {maintainer}\n"
        f"Maintainerd-Claim: {claim_id}\n"
    )


def _claim_owner(message: str) -> str | None:
    match = re.search(r"^Maintainerd-Maintainer:\s*(\S+)\s*$", message, re.M)
    return match.group(1) if match else None


def _claim_identifier(message: str) -> str | None:
    match = re.search(r"^Maintainerd-Claim:\s*(\S+)\s*$", message, re.M)
    return match.group(1) if match else None


def _pr_marker(issue_number: int, maintainer: str, claim_id: str) -> str:
    return (
        f"<!-- maintainerd implementation issue={issue_number} "
        f"maintainer={maintainer} claim={claim_id} -->"
    )


def _markdown_section(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ims)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(body or "")
    return match.group(1).strip() if match else ""


def _clip_markdown(value: str, limit: int = 3000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 32].rstrip() + "\n\n[truncated by maintainerd]"


def _pr_body(
    issue_number: int,
    maintainer: str,
    claim_id: str,
    branch: str,
    issue: dict,
    approval: dict,
    *,
    steps: list[dict] | None = None,
    status: str = "draft",
) -> str:
    steps = steps or []
    issue_body = str(issue.get("body") or "")
    title = str(issue.get("title") or f"Issue #{issue_number}").strip()
    problem = _markdown_section(issue_body, "Problem")
    direction = (
        _markdown_section(issue_body, "Possible direction")
        or _markdown_section(issue_body, "Proposal")
    )

    progress: list[str] = []
    tests: list[str] = []
    limitations: list[str] = []
    for step in steps:
        raw = step.get("result")
        if not raw:
            continue
        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(result, dict):
            continue
        summary = str(result.get("summary") or "").strip()
        commit_sha = step.get("commit_sha")
        if summary:
            prefix = f"`{str(commit_sha)[:12]}` " if commit_sha else ""
            progress.append(prefix + summary)
        for item in result.get("tests_run") or []:
            if isinstance(item, str) and item.strip():
                tests.append(item.strip())
        for item in result.get("limitations") or []:
            if isinstance(item, str) and item.strip():
                limitations.append(item.strip())

    lines = [
        _pr_marker(issue_number, maintainer, claim_id),
        "",
        "## Summary",
        "",
        f"Implements the approved fix for #{issue_number}: **{title}**.",
    ]
    if problem:
        lines.extend(["", "## Problem", "", _clip_markdown(problem)])
    if direction:
        lines.extend(["", "## Approved direction", "", _clip_markdown(direction)])
    lines.extend([
        "",
        "## Implementation progress",
        "",
    ])
    if progress:
        lines.extend(f"- {item}" for item in progress)
    else:
        lines.append(
            "Draft claimed and opened. No implementation source commit has been published yet."
        )

    lines.extend(["", "## Validation", ""])
    if tests:
        for item in dict.fromkeys(tests):
            lines.append(f"- `{item}`")
    else:
        lines.append("No implementation validation has been reported yet.")

    if limitations:
        lines.extend(["", "## Known limitations", ""])
        lines.extend(f"- {item}" for item in dict.fromkeys(limitations))

    status_text = {
        "draft": "Implementation has started and this PR remains a draft.",
        "working": "Implementation is in progress and this PR remains a draft.",
        "complete": "Implementation pass is complete and this PR is ready for review.",
        "blocked": "Implementation is blocked pending input; this PR remains a draft.",
    }.get(status, f"Implementation status: {status}.")
    lines.extend([
        "",
        "## Status",
        "",
        status_text,
        "",
        "<details>",
        "<summary>maintainerd coordination metadata</summary>",
        "",
        f"- Maintainer: `{maintainer}`",
        f"- Canonical branch: `{branch}`",
        f"- Approval comment: #{approval.get('comment_id')}",
        "- The draft PR was created before implementation source edits.",
        "- Published history is never force-pushed by maintainerd.",
        "",
        "</details>",
        "",
        f"Closes #{issue_number}",
    ])
    return "\n".join(lines).strip() + "\n"


def _steps_for_pr(state: State, implementation_id: int) -> list[dict]:
    return state.rows(
        "SELECT commit_sha,result,status,started_at FROM implementation_steps "
        "WHERE implementation_id=? AND status='completed' ORDER BY started_at,id",
        (implementation_id,),
    )


def _refresh_pr_description(
    state: State,
    active: publisher.Session,
    implementation: dict,
) -> None:
    thread = publisher.issue_thread(active, implementation["issue_number"])
    issue = thread["issue"]
    approval_rows = state.rows(
        "SELECT comment_id,author,body FROM thread_events "
        "WHERE maintainer=? AND repository=? AND comment_id=? LIMIT 1",
        (
            implementation["maintainer"],
            implementation["repository"],
            implementation["approval_comment_id"],
        ),
    )
    approval = approval_rows[0] if approval_rows else {
        "comment_id": implementation["approval_comment_id"],
        "author": implementation["approved_by"],
        "body": "",
    }
    body = _pr_body(
        implementation["issue_number"],
        implementation["maintainer"],
        implementation["claim_id"],
        implementation["branch"],
        issue,
        approval,
        steps=_steps_for_pr(state, implementation["id"]),
        status=implementation["status"],
    )
    title = str(issue.get("title") or f"Issue #{implementation['issue_number']}").strip()
    publisher.update_pull_request(
        active,
        implementation["pr_number"],
        title=title,
        body=body,
    )


def _existing_pr(active: publisher.Session, branch: str) -> dict | None:
    pulls = publisher.pulls_for_head(active, branch)
    open_pulls = [item for item in pulls if item.get("state") == "open"]
    return open_pulls[0] if open_pulls else (pulls[0] if pulls else None)


def ensure_draft(
    state: State,
    maintainer: dict,
    repository: dict,
    active: publisher.Session,
    issue_number: int,
    approval: dict,
) -> dict:
    publisher.require_implementation_permissions(active)
    existing = state.rows(
        "SELECT * FROM implementations WHERE repository=? AND issue_number=?",
        (active.repository, issue_number),
    )
    if existing:
        return existing[0]

    branch = branch_for(issue_number)
    claim_id = uuid.uuid4().hex[:16]
    branch_ref = publisher.get_ref(active, branch)
    claim_sha = None
    base_sha = None

    if branch_ref is None:
        base_ref = publisher.get_ref(active, repository["branch"])
        if base_ref is None:
            raise Error(f"Base branch {repository['branch']} was not found on GitHub.")
        base_sha = ((base_ref.get("object") or {}).get("sha"))
        if not isinstance(base_sha, str):
            raise Error("Base branch did not expose a commit SHA.")
        claim = publisher.create_empty_commit(
            active,
            base_sha,
            _claim_message(issue_number, maintainer["name"], claim_id),
        )
        claim_sha = claim["sha"]
        try:
            publisher.create_ref(active, branch, claim_sha)
        except publisher.GithubError as exc:
            if exc.status not in (409, 422):
                raise
            branch_ref = publisher.get_ref(active, branch)
            if branch_ref is None:
                raise
    if branch_ref is not None:
        claim_sha = ((branch_ref.get("object") or {}).get("sha"))
        if not isinstance(claim_sha, str):
            raise Error("Existing implementation branch did not expose a commit SHA.")
        commit = publisher.get_git_commit(active, claim_sha)
        message = str(commit.get("message") or "")
        owner = _claim_owner(message)
        recovered_claim_id = _claim_identifier(message)
        if recovered_claim_id:
            claim_id = recovered_claim_id
        if owner != maintainer["name"]:
            raise ClaimLost(
                f"Issue #{issue_number} is already claimed on {branch} by "
                f"{owner or 'another maintainer'}. No competing branch was created."
            )
        if base_sha is None:
            parents = commit.get("parents") or []
            if parents and isinstance(parents[0], dict):
                base_sha = parents[0].get("sha")

    pr = _existing_pr(active, branch)
    if pr is None:
        thread = publisher.issue_thread(active, issue_number)
        issue = thread["issue"]
        title = str(issue.get("title") or f"Issue #{issue_number}")
        pr = publisher.create_draft_pr(
            active,
            branch=branch,
            base=repository["branch"],
            title=title,
            body=_pr_body(
                issue_number,
                maintainer["name"],
                claim_id,
                branch,
                issue,
                approval,
            ),
        )
    else:
        body = str(pr.get("body") or "")
        match = re.search(r"maintainer=([^\s>]+)", body)
        if match and match.group(1) != maintainer["name"]:
            raise Error(
                f"Canonical implementation PR #{pr.get('number')} belongs to "
                f"{match.group(1)}; {maintainer['name']} will not compete."
            )

    pr_number = pr.get("number")
    pr_url = pr.get("html_url")
    if type(pr_number) is not int or not isinstance(pr_url, str):
        raise Error("Draft PR metadata was incomplete.")

    now = utcnow()
    with state.db:
        state.db.execute(
            "INSERT INTO implementations("
            "maintainer,repository,issue_number,branch,status,claim_id,claim_commit_sha,"
            "pr_number,pr_url,approval_comment_id,approved_by,base_sha,started_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                maintainer["name"], active.repository, issue_number, branch, "draft",
                claim_id, claim_sha, pr_number, pr_url, approval["comment_id"],
                approval["author"], base_sha, now, now,
            ),
        )
    row = state.rows(
        "SELECT * FROM implementations WHERE repository=? AND issue_number=?",
        (active.repository, issue_number),
    )[0]
    _refresh_pr_description(state, active, row)
    print(f"Claimed #{issue_number}; draft PR created first: {pr_url}", flush=True)
    return row


def _step_prompt(
    maintainer: dict,
    implementation: dict,
    workspace: Path,
    artifacts: Path,
) -> str:
    return f"""You are {maintainer['name']}, the current implementation owner.
Issue: #{implementation['issue_number']}
Draft PR: {implementation['pr_url']}
Branch: {implementation['branch']}

The trusted controller already won the canonical remote claim and created the
draft PR BEFORE granting this writable turn. You are now allowed to modify only
this controller-owned task worktree:

{workspace}

Read {artifacts / 'context.json'} first. It contains the issue thread, draft PR,
recent branch history, previous implementation steps, and sourced memory.

Make at most ONE coherent engineering commit worth of progress during this turn.
You may edit project files and run relevant tests inside the workspace. Do NOT
commit, push, create branches, use GitHub/network tools, or access credentials;
the controller handles Git publication after validating your result.

If one coherent step is ready, return action=commit with a concise one-line
commit_message. Also set complete=true when that commit fully resolves the
approved issue and your relevant validation is sufficient. The controller will
validate paths, commit everything in the worktree, and push that single commit
to the already-open draft PR. A final commit with complete=true ends the
implementation immediately, without a second "check whether I'm done" turn.

If the implementation was already complete before this turn and the worktree
needs no changes, return action=done with complete=true and leave the worktree
clean. If a real design/product
decision blocks safe progress, return action=needs_input, leave the worktree
clean, and explain the question in summary.

Never edit .github/*, .git/*, AGENTS.md, CLAUDE.md, or .gitmodules in this
milestone. Do not weaken tests merely to make them pass. Preserve existing
public behavior except where the approved issue explicitly requires a change.
Do not claim tests passed unless you actually ran them in this workspace.
"""


def _forbidden(paths: list[str]) -> list[str]:
    return [
        path for path in paths
        if path in FORBIDDEN_PATHS or path.startswith(FORBIDDEN_PREFIXES)
    ]


def run_step(
    state: State,
    maintainer: dict,
    repository: dict,
    active: publisher.Session,
    implementation: dict,
    lock_fd: int,
) -> str:
    if not state.can_run():
        raise Error("Daily Codex run budget reached before implementation step.")
    pr = publisher.pull_request(active, implementation["pr_number"])
    if pr.get("state") != "open":
        raise Error("Implementation PR is no longer open; autonomous writes stopped.")
    if not pr.get("draft", False):
        raise Error("Implementation PR is no longer a draft; autonomous writes stopped.")

    step_id = uuid.uuid4().hex[:16]
    artifacts = state.home / "implementations" / step_id
    artifacts.mkdir(mode=0o700)
    workspace = state.home / "workspaces" / step_id
    base_sha = ""
    safe_cleanup = True
    with state.db:
        state.db.execute(
            "INSERT INTO implementation_steps("
            "id,implementation_id,maintainer,status,started_at"
            ") VALUES (?,?,?,?,?)",
            (step_id, implementation["id"], maintainer["name"], "preparing", utcnow()),
        )
    try:
        workspace, base_sha, history = repo.prepare_remote_branch(
            state, repository, implementation["branch"], step_id
        )
        thread = publisher.issue_thread(active, implementation["issue_number"])
        previous = state.rows(
            "SELECT id,status,commit_sha,result FROM implementation_steps "
            "WHERE implementation_id=? AND id<>? ORDER BY started_at DESC LIMIT 10",
            (implementation["id"], step_id),
        )
        context = {
            "captured_at": utcnow(),
            "issue_thread": thread,
            "pull_request": pr,
            "branch_head": base_sha,
            "recent_branch_history": history,
            "previous_steps": previous,
            "memory": state.memories(maintainer["name"]),
        }
        write_json(artifacts / "context.json", context)
        write_json(artifacts / "schema.json", STEP_SCHEMA)
        prompt = _step_prompt(maintainer, implementation, workspace, artifacts)
        (artifacts / "prompt.txt").write_text(prompt, encoding="utf-8")
        version = codex.preflight(state.config)
        with state.db:
            state.db.execute(
                "UPDATE implementation_steps SET status='running',base_sha=?,invoked=1,"
                "started_at=? WHERE id=?",
                (base_sha, utcnow(), step_id),
            )
        print(
            f"{version}: implementing #{implementation['issue_number']} in workspace-write "
            f"(one commit max this turn).",
            flush=True,
        )
        codex.execute(
            state.config,
            artifacts,
            prompt,
            lock_fd,
            sandbox="workspace-write",
            workspace=workspace,
        )
        result = parse_step((artifacts / "result.json").read_text(encoding="utf-8"))
        paths = repo.changed_paths(workspace)
        forbidden = _forbidden(paths)
        if forbidden:
            raise Error("Implementation touched forbidden paths: " + ", ".join(forbidden))

        commit_sha = None
        if result["action"] == "commit":
            if not paths:
                raise Error("Codex requested a commit but made no workspace changes.")
            commit_sha = repo.commit_all(
                workspace,
                result["commit_message"].strip(),
                active.bot_login,
            )
            safe_cleanup = False
            repo.push_head(
                workspace,
                active.repository,
                implementation["branch"],
                active.token,
            )
            safe_cleanup = True
            new_status = "complete" if result["complete"] else "working"
            with state.db:
                state.db.execute(
                    "UPDATE implementations SET status=?,updated_at=? WHERE id=?",
                    (new_status, utcnow(), implementation["id"]),
                )
            print(
                f"Pushed {commit_sha[:12]} to draft PR #{implementation['pr_number']}: "
                f"{result['commit_message']}",
                flush=True,
            )
            if result["complete"]:
                print(
                    f"Implementation complete for #{implementation['issue_number']}; "
                    f"PR #{implementation['pr_number']} will be marked ready for review.",
                    flush=True,
                )
        else:
            if paths:
                raise Error(
                    f"Codex returned {result['action']} but left uncommitted changes; "
                    "worktree retained for inspection."
                )
            if result["action"] == "done":
                with state.db:
                    state.db.execute(
                        "UPDATE implementations SET status='complete',updated_at=? WHERE id=?",
                        (utcnow(), implementation["id"]),
                    )
                print(
                    f"Implementation complete for #{implementation['issue_number']}; "
                    f"PR #{implementation['pr_number']} will be marked ready for review.",
                    flush=True,
                )
            else:
                with state.db:
                    state.db.execute(
                        "UPDATE implementations SET status='blocked',updated_at=? WHERE id=?",
                        (utcnow(), implementation["id"]),
                    )
                publisher.post_comment(
                    active,
                    implementation["pr_number"],
                    result["summary"].strip(),
                )
                print(
                    f"Implementation blocked for #{implementation['issue_number']}; "
                    "question posted on the draft PR.",
                    flush=True,
                )

        with state.db:
            state.db.execute(
                "UPDATE implementation_steps SET status='completed',finished_at=?,commit_sha=?,"
                "result=? WHERE id=?",
                (utcnow(), commit_sha, json.dumps(result), step_id),
            )
            for observation in result["memory_notes"]:
                if observation.strip():
                    state.db.execute(
                        "INSERT INTO notes(maintainer,body,source,created_at) VALUES (?,?,?,?)",
                        (maintainer["name"], observation, f"implementation:{step_id}", utcnow()),
                    )
        current = state.rows(
            "SELECT * FROM implementations WHERE id=?",
            (implementation["id"],),
        )[0]
        _refresh_pr_description(state, active, current)
        if current["status"] == "complete":
            try:
                publisher.mark_ready_for_review(active, current["pr_number"])
                print(
                    f"PR #{current['pr_number']} is ready for review. Merge remains human-controlled.",
                    flush=True,
                )
            except Error as exc:
                write_json(
                    artifacts / "ready-warning.json",
                    {"message": str(exc), "pr_number": current["pr_number"]},
                )
                print(
                    f"WARN PR #{current['pr_number']} is complete but still draft: {exc}",
                    flush=True,
                )
        return step_id
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else getattr(exc, "status", "failed")
        message = str(exc) if isinstance(exc, Error) else type(exc).__name__
        with state.db:
            state.db.execute(
                "UPDATE implementation_steps SET status=?,finished_at=?,error=? WHERE id=?",
                (status, utcnow(), message, step_id),
            )
        write_json(artifacts / "failure.json", {"status": status, "message": message})
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise Error(f"Implementation step {step_id} failed: {message}. Artifacts: {artifacts}") from exc
    finally:
        usage = codex.events(artifacts / "events.jsonl")
        with state.db:
            state.db.execute(
                "UPDATE implementation_steps SET usage=? WHERE id=?",
                (json.dumps(usage), step_id),
            )
        if workspace.exists():
            try:
                if safe_cleanup and not repo.changed_paths(workspace):
                    repo.cleanup(state, repository, workspace)
                else:
                    write_json(artifacts / "retained-worktree.json", {"path": str(workspace)})
            except Error as exc:
                write_json(
                    artifacts / "cleanup-warning.json",
                    {"message": str(exc), "path": str(workspace)},
                )


def authorize(
    state: State,
    maintainer: dict,
    repository: dict,
    active: publisher.Session,
    issue_number: int,
    events: list[dict],
) -> dict | None:
    approval = approval_event(events, active.repository)
    if approval is None:
        return None

    existing = state.rows(
        "SELECT * FROM implementations WHERE repository=? AND issue_number=?",
        (active.repository, issue_number),
    )
    if existing:
        with state.db:
            state.db.execute(
                "UPDATE thread_events SET status='processed',processed_at=?,turn_id=? "
                "WHERE id=?",
                (utcnow(), f"implementation:{existing[0]['id']}", approval["id"]),
            )
        return None

    try:
        implementation = ensure_draft(
            state, maintainer, repository, active, issue_number, approval
        )
    except ClaimLost as exc:
        with state.db:
            state.db.execute(
                "UPDATE thread_events SET status='processed',processed_at=?,turn_id=? "
                "WHERE id=?",
                (utcnow(), "implementation:claimed-elsewhere", approval["id"]),
            )
        print(str(exc), flush=True)
        return None

    with state.db:
        state.db.execute(
            "UPDATE thread_events SET status='processed',processed_at=?,turn_id=? "
            "WHERE id=?",
            (utcnow(), f"implementation:{implementation['id']}", approval["id"]),
        )
    return implementation


def approval_history(
    state: State,
    maintainer: str,
    repository: str,
    issue_number: int,
) -> list[dict]:
    return state.rows(
        "SELECT * FROM thread_events WHERE maintainer=? AND repository=? "
        "AND issue_number=? AND (turn_id IS NULL OR turn_id NOT LIKE 'implementation:%') "
        "ORDER BY comment_id",
        (maintainer, repository, issue_number),
    )


def continue_one(
    state: State,
    maintainer_name: str,
    lock_fd: int,
) -> bool:
    maintainer = state.one("maintainers", maintainer_name)
    repository = state.one("repositories", maintainer["repository"])

    completed = state.rows(
        "SELECT * FROM implementations WHERE maintainer=? AND status='complete' "
        "ORDER BY updated_at DESC,id DESC LIMIT 1",
        (maintainer_name,),
    )
    if completed:
        implementation = completed[0]
        active = publisher.session_for(state, maintainer_name, implementation["repository"])
        pr = publisher.pull_request(active, implementation["pr_number"])
        if pr.get("state") == "open" and pr.get("draft", False):
            _refresh_pr_description(state, active, implementation)
            publisher.mark_ready_for_review(active, implementation["pr_number"])
            print(
                f"PR #{implementation['pr_number']} is ready for review. "
                "Merge remains human-controlled.",
                flush=True,
            )

    rows = state.rows(
        "SELECT * FROM implementations WHERE maintainer=? AND status IN ('draft','working') "
        "ORDER BY updated_at, id LIMIT 1",
        (maintainer_name,),
    )
    if not rows or not state.can_run():
        return False
    implementation = rows[0]
    active = publisher.session_for(state, maintainer_name, implementation["repository"])
    publisher.require_implementation_permissions(active)
    run_step(state, maintainer, repository, active, implementation, lock_fd)
    return True
