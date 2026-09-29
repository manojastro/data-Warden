"""Optional GitHub adapter: read recent diffs and open a scoped PR for an *approved* repair.

Disabled unless DW_GITHUB_TOKEN and DW_GITHUB_REPO are set. Local git workspace diffs and patch
artifacts are used otherwise. Not exercised against a live repository in this build (no token was
configured); see docs/BUILD_STATUS.md.
"""

from __future__ import annotations

import base64
import re

import httpx

from datawarden.config import get_settings

API = "https://api.github.com"
ALLOWED_PATH = re.compile(r"^(pipelines/dbt/models/(staging|marts)/[a-z_]+\.sql|pipelines/ingestion/mappings\.yaml)$")


class GithubDisabled(RuntimeError):
    pass


def _client() -> tuple[httpx.Client, str]:
    s = get_settings()
    token, repo = s.github_token.get_secret_value(), s.github_repo
    if not token or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise GithubDisabled("GitHub integration disabled (DW_GITHUB_TOKEN / DW_GITHUB_REPO not set)")
    return httpx.Client(
        base_url=API, timeout=20, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    ), repo


def recent_commits(path: str = "pipelines/dbt", limit: int = 5) -> list[dict]:
    client, repo = _client()
    with client:
        r = client.get(f"/repos/{repo}/commits", params={"path": path, "per_page": min(limit, 20)})
        r.raise_for_status()
        return [
            {"sha": c["sha"], "message": c["commit"]["message"][:200], "date": c["commit"]["author"]["date"]}
            for c in r.json()
        ]


def open_repair_pr(*, branch: str, base: str, files: dict[str, str], title: str, body: str) -> dict:
    """Open a PR containing exactly the approved files. Only repair-allowlisted paths are accepted."""
    bad = [p for p in files if not ALLOWED_PATH.match(p)]
    if bad:
        raise ValueError(f"paths outside the repair allowlist: {bad}")
    client, repo = _client()
    with client:
        base_sha = client.get(f"/repos/{repo}/git/ref/heads/{base}").raise_for_status().json()["object"]["sha"]
        client.post(f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha}).raise_for_status()
        for path, content in files.items():
            cur = client.get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
            payload = {"message": title[:72], "branch": branch, "content": base64.b64encode(content.encode()).decode()}
            if cur.status_code == 200:
                payload["sha"] = cur.json()["sha"]
            client.put(f"/repos/{repo}/contents/{path}", json=payload).raise_for_status()
        pr = client.post(f"/repos/{repo}/pulls", json={"title": title, "head": branch, "base": base, "body": body})
        pr.raise_for_status()
        return {"number": pr.json()["number"], "url": pr.json()["html_url"]}
