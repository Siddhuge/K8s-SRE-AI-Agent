"""CI/CD tools — GitLab pipelines and GitHub Actions runs (read-only).

Recent failed pipelines and deploy timing are key correlation inputs: a deploy that
shipped a bad config or image at T-0 is the prime suspect for a regression at T+2m.
"""
from __future__ import annotations

from urllib.parse import quote

from ..config import get_settings
from ._http import get_json


def _gh_headers(settings) -> dict:
    """GitHub API headers. Omit Authorization when no token is configured — an empty
    'Bearer ' is rejected (401), whereas unauthenticated requests work for public
    repos (at a lower rate limit)."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def _gl_headers(settings) -> dict:
    return {"PRIVATE-TOKEN": settings.gitlab_token} if settings.gitlab_token else {}


def register(mcp) -> None:
    @mcp.tool()
    def gitlab_pipelines(project: str, ref: str = "", status: str = "", limit: int = 10) -> list[dict]:
        """Recent GitLab CI pipelines for a project (path or numeric ID). Filter by
        `status` (failed/success/running) and `ref` (branch/tag)."""
        settings = get_settings()
        pid = quote(project, safe="")
        params = {"per_page": str(limit)}
        if ref:
            params["ref"] = ref
        if status:
            params["status"] = status
        headers = _gl_headers(settings)
        data = get_json(f"{settings.gitlab_url.rstrip('/')}/api/v4/projects/{pid}/pipelines", params=params, headers=headers)
        return [
            {"id": p["id"], "status": p["status"], "ref": p["ref"], "sha": p["sha"][:8], "created_at": p["created_at"], "web_url": p["web_url"]}
            for p in (data if isinstance(data, list) else [])
        ]

    @mcp.tool()
    def github_actions_runs(repo: str, workflow: str = "", branch: str = "", limit: int = 10) -> list[dict]:
        """Recent GitHub Actions workflow runs for `owner/repo`. Optionally filter to a
        workflow file (e.g. deploy.yml) or branch."""
        settings = get_settings()
        headers = _gh_headers(settings)
        path = f"actions/workflows/{workflow}/runs" if workflow else "actions/runs"
        params = {"per_page": str(limit)}
        if branch:
            params["branch"] = branch
        data = get_json(f"https://api.github.com/repos/{repo}/{path}", params=params, headers=headers)
        return [
            {"id": r["id"], "name": r["name"], "status": r["status"], "conclusion": r["conclusion"],
             "head_sha": r["head_sha"][:8], "created_at": r["created_at"], "url": r["html_url"]}
            for r in data.get("workflow_runs", [])
        ]

    @mcp.tool()
    def recent_deployments(repo: str, environment: str = "", limit: int = 10) -> list[dict]:
        """GitHub deployments timeline for a repo/environment — the authoritative
        'what shipped when' for change-correlation."""
        settings = get_settings()
        headers = _gh_headers(settings)
        params = {"per_page": str(limit)}
        if environment:
            params["environment"] = environment
        data = get_json(f"https://api.github.com/repos/{repo}/deployments", params=params, headers=headers)
        return [
            {"id": d["id"], "environment": d["environment"], "ref": d["ref"], "sha": d["sha"][:8], "created_at": d["created_at"]}
            for d in (data if isinstance(data, list) else [])
        ]

    @mcp.tool()
    def compare_deployments(repo: str, base_sha: str, head_sha: str) -> dict:
        """Diff two deployed revisions: commits + changed files between a known-good
        SHA and the failing one. Surfaces exactly what changed in the bad release."""
        settings = get_settings()
        headers = _gh_headers(settings)
        data = get_json(f"https://api.github.com/repos/{repo}/compare/{base_sha}...{head_sha}", headers=headers)
        return {
            "ahead_by": data.get("ahead_by"),
            "commits": [{"sha": c["sha"][:8], "message": c["commit"]["message"].splitlines()[0]} for c in data.get("commits", [])],
            "changed_files": [f["filename"] for f in data.get("files", [])][:50],
        }
