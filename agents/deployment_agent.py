"""Deployment agent (Qwen3 via Ollama).

Deploys a repo (mode="initial") or a branch for preview (mode="preview").
The orchestrator passes `mode` and routes the result itself — the agent
does not need to know who is consuming its output.
"""
import json
import os
import re
import time

import requests
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from schemas import DeploymentResult

VERCEL_TOKEN = os.environ["VERCEL_TOKEN"]
VERCEL_TEAM_ID = os.environ.get("VERCEL_TEAM_ID")  # optional
VERCEL_PROJECT_NAME = os.environ["VERCEL_PROJECT_NAME"]
VERCEL_BYPASS_SECRET = os.environ.get("VERCEL_BYPASS_SECRET")  # optional, needed if Deployment Protection is on
GITHUB_REPO = os.environ["GITHUB_REPO"]  # format: "owner/repo"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional, raises GitHub's rate limit if set
# Path hit by the post-deploy health check. "/" is often served as a static file by
# Vercel's CDN without ever invoking your serverless function — point this at a real
# API route so the check actually exercises your server code.
HEALTH_CHECK_PATH = os.environ.get("HEALTH_CHECK_PATH", "/")

BASE_URL = "https://api.vercel.com"
HEADERS = {"Authorization": f"Bearer {VERCEL_TOKEN}", "Content-Type": "application/json"}
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 600

_repo_id_cache: int | None = None
_project_id_cache: str | None = None


def _team_params() -> dict:
    return {"teamId": VERCEL_TEAM_ID} if VERCEL_TEAM_ID else {}


def _get_github_repo_id() -> int:
    """Vercel's gitSource needs GitHub's numeric repo id, not the 'owner/repo' string. Cached after first lookup."""
    global _repo_id_cache
    if _repo_id_cache is not None:
        return _repo_id_cache
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    resp = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}", headers=headers, timeout=30)
    resp.raise_for_status()
    _repo_id_cache = resp.json()["id"]
    return _repo_id_cache


def _get_vercel_project_id() -> str:
    """Runtime-logs endpoint needs Vercel's projectId, not the project name. Cached after first lookup."""
    global _project_id_cache
    if _project_id_cache is not None:
        return _project_id_cache
    resp = requests.get(
        f"{BASE_URL}/v9/projects/{VERCEL_PROJECT_NAME}",
        headers=HEADERS,
        params=_team_params(),
        timeout=30,
    )
    _raise_with_body(resp)
    _project_id_cache = resp.json()["id"]
    return _project_id_cache


def _raise_with_body(resp: requests.Response) -> None:
    """Like resp.raise_for_status(), but includes Vercel's JSON error body in the message."""
    if resp.ok:
        return
    try:
        detail = resp.json()
    except ValueError:
        detail = resp.text
    raise requests.exceptions.HTTPError(
        f"{resp.status_code} error for {resp.url}: {detail}", response=resp
    )


def _create_deployment(ref: str) -> str:
    """Creates a deployment from a git ref (branch name). Returns deployment id."""
    payload = {
        "name": VERCEL_PROJECT_NAME,
        "gitSource": {"type": "github", "repoId": _get_github_repo_id(), "ref": ref},
    }
    resp = requests.post(
        f"{BASE_URL}/v13/deployments",
        headers=HEADERS,
        params=_team_params(),
        json=payload,
        timeout=30,
    )
    _raise_with_body(resp)
    return resp.json()["id"]


def _poll_deployment(deployment_id: str) -> dict:
    """Polls until the deployment reaches a terminal state. Returns the final deployment object."""
    elapsed = 0
    while elapsed < POLL_TIMEOUT_SECONDS:
        resp = requests.get(
            f"{BASE_URL}/v13/deployments/{deployment_id}",
            headers=HEADERS,
            params=_team_params(),
            timeout=30,
        )
        _raise_with_body(resp)
        data = resp.json()
        ready_state = data.get("readyState")
        if ready_state in ("READY", "ERROR", "CANCELED"):
            return data
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS
    raise TimeoutError(f"Deployment {deployment_id} did not finish within {POLL_TIMEOUT_SECONDS}s")


def _fetch_error_log(deployment_id: str) -> str:
    """Fetches deployment events and extracts lines that look like errors, joined as one excerpt."""
    resp = requests.get(
        f"{BASE_URL}/v3/deployments/{deployment_id}/events",
        headers=HEADERS,
        params=_team_params(),
        timeout=30,
    )
    _raise_with_body(resp)
    events = resp.json()
    lines = [e.get("text", "") for e in events if e.get("text")]
    full_log = "\n".join(lines)
    error_lines = [l for l in lines if re.search(r"error|does not exist|fail", l, re.IGNORECASE)]
    return "\n".join(error_lines) if error_lines else full_log[-4000:]


def _fetch_runtime_logs(deployment_id: str, max_lines: int = 50) -> str:
    """Queries Vercel's runtime-logs endpoint (application/stream+json: one JSON object
    per line) and returns error/fatal level entries. This catches crashes that only
    happen when the deployed app is actually invoked (build can succeed, then the
    function 500s on a real request) — distinct from _fetch_error_log, which only
    covers build-time events.
    """
    resp = requests.get(
        f"{BASE_URL}/v1/projects/{_get_vercel_project_id()}/deployments/{deployment_id}/runtime-logs",
        headers=HEADERS,
        params=_team_params(),
        timeout=30,
        stream=True,
    )
    _raise_with_body(resp)
    error_lines = []
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except ValueError:
            continue
        if entry.get("level") in ("error", "fatal"):
            error_lines.append(
                f"[{entry.get('level')}] {entry.get('requestPath', '')} "
                f"({entry.get('responseStatusCode', '')}): {entry.get('message', '')}"
            )
        if len(error_lines) >= max_lines:
            break
    return "\n".join(error_lines)


def _health_check(deployment_id: str, deployment_url: str, retries: int = 3, delay_seconds: int = 5) -> tuple[bool, str]:
    """Hits the deployed URL after build success. A successful build can still crash at
    runtime (serverless functions execute on-demand), so this catches that case.
    Returns (ok, detail) — detail is pulled from Vercel's runtime-logs endpoint when
    available, falling back to the raw HTTP response body if no log lines come back yet.
    """
    last_detail = ""
    for attempt in range(retries):
        try:
            req_headers = {"x-vercel-protection-bypass": VERCEL_BYPASS_SECRET} if VERCEL_BYPASS_SECRET else {}
            resp = requests.get(deployment_url, headers=req_headers, timeout=30)
        except requests.exceptions.RequestException as e:
            last_detail = f"Request to {deployment_url} failed: {e}"
            time.sleep(delay_seconds)
            continue
        print(f"HEALTH CHECK: GET {deployment_url} -> status {resp.status_code}, bypass_secret_set={bool(VERCEL_BYPASS_SECRET)}")
        if resp.status_code < 500:
            return True, ""
        time.sleep(delay_seconds)  # let the runtime log catch up before reading it
        runtime_logs = _fetch_runtime_logs(deployment_id)
        last_detail = runtime_logs if runtime_logs else f"GET {deployment_url} returned {resp.status_code}: {resp.text[:2000]}"
        time.sleep(delay_seconds)
    return False, last_detail


def trigger_deployment(target: str, mode: str) -> dict:
    """Triggers a deployment, polls until build finishes, then health-checks the live URL
    to catch runtime errors a successful build can still hide (serverless functions only
    execute on-demand, so a clean build does not guarantee the app runs without crashing).

    Args:
        target: branch name to deploy. For mode="initial" use "main" (or your default branch).
                For mode="preview" use the fix branch name.
        mode: "initial" or "preview".

    Returns:
        dict with success, deployment_url, raw_log_excerpt.
    """
    deployment_id = _create_deployment(ref=target)
    final = _poll_deployment(deployment_id)

    if final.get("readyState") != "READY":
        return {
            "success": False,
            "deployment_url": None,
            "raw_log_excerpt": _fetch_error_log(deployment_id),
        }

    deployment_url = f"https://{final.get('url')}"
    health_check_url = deployment_url.rstrip("/") + HEALTH_CHECK_PATH
    healthy, detail = _health_check(deployment_id, health_check_url)
    if healthy:
        return {"success": True, "deployment_url": deployment_url, "raw_log_excerpt": None}
    else:
        return {"success": False, "deployment_url": None, "raw_log_excerpt": detail}


deployment_agent = LlmAgent(
    name="deployment_agent",
    model=LiteLlm(model="ollama_chat/qwen3"),
    description="Deploys a repo branch and reports success/failure with logs on failure.",
    instruction=(
        "Call trigger_deployment with the given target and mode. "
        "Return the result exactly as the tool gives it."
    ),
    tools=[trigger_deployment],
    output_schema=DeploymentResult,
    output_key="deployment_result",
)