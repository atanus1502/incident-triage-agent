from __future__ import annotations

import re
from typing import Any

import httpx

from triage.config import Settings
from triage.permissions import ReadPolicy, read_only_client
from triage.tools.base import TIME_RANGE, Tool, ToolError, parse_time

_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_REF = re.compile(r"^[\w./-]{1,200}$")


class GitHubTools:
    def __init__(self, settings: Settings, allowed_repos: set[str], transport: httpx.BaseTransport | None = None):
        # Repos are restricted to the service catalog so the model can't wander the org.
        self.allowed_repos = allowed_repos
        self.http = read_only_client(
            settings.github_api_url,
            ReadPolicy(),
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=transport,
        )

    def _repo(self, repo: str) -> str:
        if repo not in self.allowed_repos:
            raise ToolError(f"Repo {repo!r} is not in the service catalog. Allowed: {', '.join(sorted(self.allowed_repos))}")
        return repo

    def list_deployments(self, repo: str, environment: str, start: str, end: str) -> list[dict[str, Any]]:
        lo, hi = parse_time(start), parse_time(end)
        r = self.http.get(f"/repos/{self._repo(repo)}/deployments", params={"environment": environment, "per_page": 50})
        r.raise_for_status()
        out = []
        for d in r.json():
            created = parse_time(d["created_at"])
            if lo <= created <= hi:
                out.append({
                    "id": d["id"], "sha": d["sha"][:12], "ref": d.get("ref"), "created_at": d["created_at"],
                    "creator": (d.get("creator") or {}).get("login"), "description": d.get("description"),
                })
        return out

    def list_commits(self, repo: str, start: str, end: str, branch: str = "main") -> list[dict[str, Any]]:
        if not _REF.match(branch):
            raise ToolError("Invalid branch name")
        r = self.http.get(
            f"/repos/{self._repo(repo)}/commits",
            params={"sha": branch, "since": parse_time(start).isoformat(), "until": parse_time(end).isoformat(), "per_page": 50},
        )
        r.raise_for_status()
        return [
            {"sha": c["sha"][:12], "date": c["commit"]["author"]["date"], "author": c["commit"]["author"]["name"],
             "message": c["commit"]["message"].splitlines()[0][:200]}
            for c in r.json()
        ]

    def compare(self, repo: str, base: str, head: str) -> dict[str, Any]:
        for ref in (base, head):
            if not _REF.match(ref):
                raise ToolError(f"Invalid ref {ref!r}")
        r = self.http.get(f"/repos/{self._repo(repo)}/compare/{base}...{head}")
        r.raise_for_status()
        data = r.json()
        return {
            "total_commits": data.get("total_commits"),
            "commits": [
                {"sha": c["sha"][:12], "author": c["commit"]["author"]["name"], "message": c["commit"]["message"].splitlines()[0][:200]}
                for c in data.get("commits", [])[-30:]
            ],
            "files": [
                {"file": f["filename"], "status": f["status"], "additions": f["additions"], "deletions": f["deletions"],
                 "patch": (f.get("patch") or "")[:1500]}
                for f in data.get("files", [])[:40]
            ],
        }

    def pulls_for_commit(self, repo: str, sha: str) -> list[dict[str, Any]]:
        if not _SHA.match(sha):
            raise ToolError("sha must be 7-40 hex characters")
        r = self.http.get(f"/repos/{self._repo(repo)}/commits/{sha}/pulls")
        r.raise_for_status()
        return [
            {"number": p["number"], "title": p["title"], "author": (p.get("user") or {}).get("login"),
             "merged_at": p.get("merged_at"), "url": p.get("html_url"), "body": (p.get("body") or "")[:2000]}
            for p in r.json()
        ]

    def tools(self) -> list[Tool]:
        repo = {"type": "string", "description": "owner/name, from lookup_service"}
        return [
            Tool(
                name="github_deployments",
                description="List GitHub deployments of a repo to an environment within a time range. Use to find what shipped shortly before the alert.",
                properties={"repo": repo, "environment": {"type": "string"}, **TIME_RANGE},
                required=("repo", "environment", "start", "end"),
                handler=self.list_deployments,
            ),
            Tool(
                name="github_commits",
                description="List commits on a branch (default main) within a time range.",
                properties={"repo": repo, **TIME_RANGE, "branch": {"type": "string"}},
                required=("repo", "start", "end"),
                handler=self.list_commits,
            ),
            Tool(
                name="github_compare",
                description=(
                    "Diff two refs (SHAs, tags or branches): commits in between and changed files with truncated patches. "
                    "Use base = previous deployment sha, head = suspect deployment sha."
                ),
                properties={"repo": repo, "base": {"type": "string"}, "head": {"type": "string"}},
                required=("repo", "base", "head"),
                handler=self.compare,
            ),
            Tool(
                name="github_pulls_for_commit",
                description="Get the pull request(s) that introduced a commit, including title and description.",
                properties={"repo": repo, "sha": {"type": "string"}},
                required=("repo", "sha"),
                handler=self.pulls_for_commit,
            ),
        ]
