"""Optional, bounded GitHub snapshots using the host's gh login. GET only."""

from __future__ import annotations

import json
import os
import shutil

from .repo import command
from .state import Error, utcnow


def snapshot(repository: str | None, enabled: bool) -> dict:
    data = {"repository": repository, "captured_at": utcnow(), "open_items": [],
            "recent_closed_items": [], "workflow_runs": [], "limitations": []}
    limitations = data["limitations"]
    if not enabled or not repository:
        limitations.append("GitHub context disabled or no GitHub repository configured. Duplicate checks are incomplete.")
        return data
    if not shutil.which("gh"):
        limitations.append("gh is not installed. Issues, PRs, discussions and CI were not fetched.")
        return data
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_PAGER="cat")

    def get(endpoint: str):
        raw = command(["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint],
                      env=env, timeout=30)
        if len(raw) > 4_000_000:
            raise Error("GitHub response exceeds the 4 MB snapshot limit.")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Error("GitHub response was not JSON.") from exc

    try:
        items = get(f"repos/{repository}/issues?state=open&sort=updated&direction=desc&per_page=100")
        if not isinstance(items, list):
            raise Error("GitHub issues response was not a list.")
        if len(items) == 100:
            limitations.append("Open items capped at 100. Additional open issues or PRs may exist.")
        for item in items:
            body = item.get("body") or ""
            entry = {key: item.get(key) for key in ("number", "title", "html_url", "updated_at")}
            entry.update(kind="pull_request" if "pull_request" in item else "issue",
                         author=(item.get("user") or {}).get("login"), body=body[:12000],
                         labels=[label.get("name") for label in item.get("labels", [])], comments=[])
            if len(body) > 12000:
                limitations.append(f"Body of #{item['number']} truncated at 12000 characters.")
            data["open_items"].append(entry)
        # A snapshot is a starting point, not a claim to have read all discussion.
        for entry in data["open_items"][:5]:
            comments = get(f"repos/{repository}/issues/{entry['number']}/comments?per_page=20")
            if not isinstance(comments, list):
                raise Error("GitHub comments response was not a list.")
            entry["comments"] = [
                {"author": (item.get("user") or {}).get("login"), "body": (item.get("body") or "")[:8000],
                 "created_at": item.get("created_at"), "url": item.get("html_url")}
                for item in comments
            ]
            if len(comments) == 20:
                limitations.append(f"Comments on #{entry['number']} capped at the first 20; recent replies may be missing.")
            if any(len(item.get("body") or "") > 8000 for item in comments):
                limitations.append(f"Some comments on #{entry['number']} were truncated at 8000 characters.")
        closed = get(f"repos/{repository}/issues?state=closed&sort=updated&direction=desc&per_page=30")
        if not isinstance(closed, list):
            raise Error("GitHub closed-items response was not a list.")
        data["recent_closed_items"] = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "html_url": item.get("html_url"),
                "updated_at": item.get("updated_at"),
                "closed_at": item.get("closed_at"),
                "kind": "pull_request" if "pull_request" in item else "issue",
                "author": (item.get("user") or {}).get("login"),
                "body": (item.get("body") or "")[:8000],
                "labels": [label.get("name") for label in item.get("labels", [])],
            }
            for item in closed
        ]
        if len(closed) == 30:
            limitations.append("Recent closed items capped at 30; older decisions may be missing.")
        limitations.append("Conversation comments sampled for the five most recently updated open items only. "
                           "PR diffs, inline reviews and GitHub Discussions are not included.")
    except Error as exc:
        limitations.append(f"Issue/PR snapshot incomplete: {exc}")
    try:
        runs = get(f"repos/{repository}/actions/runs?per_page=10").get("workflow_runs", [])
        data["workflow_runs"] = [
            {key: run.get(key) for key in ("name", "status", "conclusion", "head_sha", "html_url")}
            for run in runs
        ]
    except (Error, AttributeError) as exc:
        limitations.append(f"CI metadata unavailable: {exc}")
    return data
