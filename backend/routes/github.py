"""GitHub config + test + push endpoints."""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import requests
from requests.auth import HTTPBasicAuth

from db import projects, srs_documents, audit_log
from models import GithubPushRequest

router = APIRouter(prefix="/github", tags=["github"])


def _ssl_verify():
    """Resolve SSL verify policy for outbound HTTPS calls.

    Honours the same env vars documented in CLAUDE.md for httpx:
      - LAMA_DISABLE_SSL_VERIFY=1  → verify=False (corporate MITM proxies)
      - LAMA_CA_BUNDLE=/path/ca.pem → verify=<path>
    Returns a value suitable for `requests`'s `verify=` kwarg (bool or str).
    """
    if os.getenv("LAMA_DISABLE_SSL_VERIFY", "").strip() in ("1", "true", "True", "yes"):
        # Silence noisy warnings when the user explicitly opts out.
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        return False
    bundle = os.getenv("LAMA_CA_BUNDLE", "").strip()
    if bundle:
        return bundle
    return True


class GithubConfig(BaseModel):
    project_id: str
    repo_url: str
    # Either provide a personal access token, OR username + password.
    token: str = ""
    username: str = ""
    password: str = ""
    branch: str = "main"


class GithubTestRequest(BaseModel):
    repo_url: str = ""
    token: str = ""
    username: str = ""
    password: str = ""


def _parse_repo(repo_url: str) -> tuple[str, str]:
    """Parse 'https://github.com/owner/repo[.git]' into (owner, repo)."""
    url = (repo_url or "").strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = url.replace("https://", "").replace("http://", "").split("/")
    # github.com / owner / repo
    if len(parts) < 3 or "github" not in parts[0]:
        raise HTTPException(400, "Invalid GitHub repo URL")
    return parts[-2], parts[-1]


def _resolve_auth(token: str, username: str, password: str):
    """Return (auth_kind, auth_value) for downstream callers.

    auth_kind: "token" | "basic" | None
    auth_value: token string OR (username, password) tuple
    """
    if token:
        return "token", token
    if username and password:
        return "basic", (username, password)
    return None, None


@router.post("/config")
async def save_config(payload: GithubConfig):
    """Save GitHub config to the project document.

    Accepts EITHER a personal access token OR a username+password pair.
    At least one credential method must be provided.
    """
    if not payload.token and not (payload.username and payload.password):
        raise HTTPException(
            400,
            "Provide either a personal access token, or both a username and password.",
        )
    proj = await projects.find_one({"id": payload.project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    update_doc = {
        "github_repo": payload.repo_url,
        "github_branch": payload.branch,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Only overwrite credentials that the caller actually supplied. This lets
    # the user update the repo/branch without re-entering their secret.
    if payload.token:
        update_doc["github_token"] = payload.token
        # Switching to token auth clears stale basic-auth creds.
        update_doc["github_username"] = ""
        update_doc["github_password"] = ""
    elif payload.username and payload.password:
        update_doc["github_username"] = payload.username
        update_doc["github_password"] = payload.password
        update_doc["github_token"] = ""

    await projects.update_one({"id": payload.project_id}, {"$set": update_doc})
    return {"ok": True}


@router.get("/config/{project_id}")
async def get_config(project_id: str):
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    token = proj.get("github_token", "")
    username = proj.get("github_username", "")
    password = proj.get("github_password", "")
    return {
        "repo_url": proj.get("github_repo", ""),
        "branch": proj.get("github_branch", "main"),
        "has_token": bool(token),
        "token_preview": (token[:6] + "…") if token else "",
        "username": username,
        "has_password": bool(password),
        "auth_method": "token" if token else ("basic" if username and password else ""),
    }


@router.post("/test")
async def test_connection(payload: GithubTestRequest):
    """Fetch repo metadata to verify credentials + URL."""
    auth_kind, auth_value = _resolve_auth(payload.token, payload.username, payload.password)
    if not auth_kind:
        return {"ok": False, "error": "Provide either a token, or username + password."}

    if not payload.repo_url:
        # Credential-only validation: format-only check for tokens.
        if auth_kind == "token":
            t = auth_value or ""
            if not (t.startswith("ghp_") or t.startswith("github_pat_") or len(t) > 20):
                return {"ok": False, "error": "Invalid token format"}
        return {"ok": True, "message": "Credential format valid. Provide repo_url to test connection."}
    try:
        owner, repo = _parse_repo(payload.repo_url)
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    try:
        headers = {"Accept": "application/vnd.github+json"}
        auth = None
        if auth_kind == "token":
            headers["Authorization"] = f"token {auth_value}"
        else:
            auth = HTTPBasicAuth(auth_value[0], auth_value[1])
        verify = _ssl_verify()
        r = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers,
            auth=auth,
            timeout=15,
            verify=verify,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if r.status_code == 200:
        data = r.json()
        return {
            "ok": True,
            "repo_name": data.get("full_name"),
            "private": data.get("private"),
            "default_branch": data.get("default_branch"),
            "auth_method": auth_kind,
        }

    # Non-200: disambiguate auth failure vs. genuinely missing repo by
    # probing /user with the same credentials. GitHub returns 404 (not 401)
    # for private repos when the caller is unauthorised, so a bare 404 is
    # ambiguous on its own.
    status = r.status_code
    body_preview = r.text[:200]
    try:
        probe = requests.get(
            "https://api.github.com/user",
            headers=headers,
            auth=auth,
            timeout=10,
            verify=verify,
        )
        auth_ok = probe.status_code == 200
        probe_status = probe.status_code
    except Exception:
        auth_ok = False
        probe_status = None

    if not auth_ok:
        if auth_kind == "basic":
            hint = (
                "Authentication failed. GitHub deprecated password "
                "authentication in August 2021 — basic auth with your "
                "account password no longer works against api.github.com. "
                "Create a Personal Access Token (Settings → Developer "
                "settings → Personal access tokens) and use the "
                "'Personal Access Token' option above. If your org uses "
                "SAML SSO, also authorise the token for the org."
            )
        else:
            hint = (
                "Authentication failed — the token is invalid, expired, "
                "lacks `repo` scope, or (for SAML-SSO orgs) has not been "
                "authorised for the organisation."
            )
        return {
            "ok": False,
            "error": f"{hint} (repos API {status}, /user probe {probe_status})",
        }

    # Auth is good → 404 truly means repo not found / no access.
    if status == 404:
        return {
            "ok": False,
            "error": (
                f"Repository '{owner}/{repo}' not found, or your account "
                "has no access. Verify the URL, that the repo exists, and "
                "(for org repos) that you have at least read permission."
            ),
        }
    return {"ok": False, "error": f"GitHub API {status}: {body_preview}"}


def _srs_to_markdown(project: dict, srs: dict) -> str:
    # Import lazily to avoid circular imports at module-load time.
    from routes.srs import SECTION_CONFIGS, LEGACY_SECTION_ALIAS
    sec = dict(srs.get("sections", {}) or {})
    # Migrate legacy keys into the new key space so SRSes generated before
    # the IEEE 29148 restructure still export with proper section labels.
    for old_key, new_key in LEGACY_SECTION_ALIAS.items():
        if old_key in sec and not sec.get(new_key):
            sec[new_key] = sec[old_key]
    # Render every section EXCEPT the deterministic ER (JSON, not markdown).
    order = [(c["key"], c["label"]) for c in SECTION_CONFIGS if c["key"] != "entity_model"]
    lines = [
        f"# Software Requirements Specification — {project.get('name','')}",
        "",
        f"- **Source:** {project.get('source_tech','')}",
        f"- **Target:** {project.get('target_tech','')}",
        f"- **Version:** {srs.get('version', 1)}",
        f"- **Frozen:** {srs.get('frozen', False)}  ({srs.get('frozen_at','')})",
        "",
        "---",
        "",
    ]
    for key, label in order:
        lines.append(f"## {label}\n\n{sec.get(key, '') or '_(empty)_'}\n")
    return "\n".join(lines)


@router.post("/push")
async def push_to_github(payload: GithubPushRequest):
    """Push SRS markdown to GitHub. Stage 2-4 pushes are stubbed."""
    proj = await projects.find_one({"id": payload.project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    repo_url = payload.repo_url or proj.get("github_repo", "")
    token = payload.token or proj.get("github_token", "")
    username = payload.username or proj.get("github_username", "")
    password = payload.password or proj.get("github_password", "")
    branch = payload.branch or proj.get("github_branch", "main")

    auth_kind, auth_value = _resolve_auth(token, username, password)
    if not repo_url or not auth_kind:
        return {
            "status": "error",
            "message": "GitHub repo and credentials (token OR username+password) must be configured in Settings.",
        }

    srs = await srs_documents.find_one({"project_id": payload.project_id}, {"_id": 0})
    if not srs:
        return {"status": "error", "message": "No SRS to push — generate and freeze the SRS first."}

    try:
        from github import Github
        owner, repo_name = _parse_repo(repo_url)
        verify = _ssl_verify()
        if auth_kind == "token":
            gh = Github(auth_value, verify=verify)
        else:
            gh = Github(auth_value[0], auth_value[1], verify=verify)
        repo = gh.get_repo(f"{owner}/{repo_name}")
        content = _srs_to_markdown(proj, srs)
        path = "docs/SRS.md"
        try:
            existing = repo.get_contents(path, ref=branch)
            repo.update_file(path, "LAMA: update SRS", content, existing.sha, branch=branch)
            action = "updated"
        except Exception:
            repo.create_file(path, "LAMA: add SRS", content, branch=branch)
            action = "created"
    except Exception as e:
        return {"status": "error", "message": f"GitHub push failed: {e}"}

    await audit_log.insert_one({
        "action": "github.push",
        "project_id": payload.project_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {"path": path, "action": action, "branch": branch, "auth_method": auth_kind},
    })

    return {
        "status": "success",
        "message": f"SRS {action} at docs/SRS.md on branch {branch}",
        "project_id": payload.project_id,
        "repo_url": repo_url,
        "branch": branch,
        "auth_method": auth_kind,
    }
