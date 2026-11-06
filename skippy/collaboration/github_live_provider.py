"""
SKIPPY AI Collaboration Bridge — Authenticated Read-Only GitHub Provider.

FOUNDATION ONLY.
No live connection is enabled by default.
No credentials are stored in source.
No repository writes. No trading.

A fine-grained PAT restricted to SGGlobal/QLA with read permissions for
Metadata, Contents, Pull requests, and Actions is the supported credential.
GitHub App installation auth is a future alternative, not implemented here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

from skippy.collaboration.github_adapter import (
    ALLOWED_REPOSITORY,
    CIResultSnapshot,
    CIStatus,
    AdapterError,
    BranchSnapshot,
    CommitSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
    WorkflowSnapshot,
)


GITHUB_API_HOST = "https://api.github.com"
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 10


class TransportError(AdapterError):
    """Raised when the HTTP transport cannot complete a request."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: dict[str, str]


# Transport signature: (method, url, headers, timeout) -> HTTPResponse
HTTPTransport = Callable[[str, str, dict[str, str], int], HTTPResponse]


class GitHubLiveProvider:
    """
    Read-only GitHub REST provider.

    Uses an injectable transport so tests never touch the network.
    GET only. Fixed API host. Bounded size and timeout. No retries.
    """

    def __init__(self, token: str, transport: HTTPTransport):
        if not isinstance(token, str) or not token.strip():
            raise AdapterError("Missing GitHub token")
        if token.lower().startswith("bearer ") or " " in token:
            raise AdapterError("Malformed GitHub token")

        self._token = token
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "QLA-Collaboration-Bridge",
        }

    def _get(self, path: str) -> dict:
        if not path.startswith("/"):
            raise AdapterError("Invalid API path")

        url = f"{GITHUB_API_HOST}{path}"
        if not url.startswith(GITHUB_API_HOST):
            raise AdapterError("Unexpected API host")

        try:
            response = self._transport(
                "GET", url, self._headers(), REQUEST_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise TransportError("GitHub request timed out") from exc
        except Exception as exc:
            raise TransportError("GitHub request failed") from exc

        if response.status in {301, 302, 303, 307, 308}:
            raise TransportError("Unexpected redirect")

        if response.status == 401:
            raise TransportError("Authentication failed")
        if response.status == 403:
            raise TransportError("Insufficient permissions or forbidden")
        if response.status == 404:
            raise AdapterError("Resource not found")
        if response.status == 429:
            raise TransportError("GitHub rate limit exceeded")
        if response.status >= 500:
            raise TransportError("GitHub server error")
        if response.status != 200:
            raise TransportError(f"Unexpected GitHub status {response.status}")

        if len(response.body) > MAX_RESPONSE_BYTES:
            raise TransportError("Response exceeds size bound")

        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError("Malformed JSON") from exc

        if not isinstance(payload, (dict, list)):
            raise TransportError("Unexpected response type")

        return payload

    def _check_repository(self, repository: str) -> tuple[str, str]:
        if repository != ALLOWED_REPOSITORY:
            raise AdapterError("Repository is not on the allowlist")
        owner, name = repository.split("/", 1)
        return owner, name

    def repository(self, repository: str) -> RepositorySnapshot:
        owner, name = self._check_repository(repository)
        data = self._get(f"/repos/{owner}/{name}")

        if not isinstance(data, dict):
            raise AdapterError("Malformed repository response")

        full = data.get("full_name")
        if full != repository:
            raise AdapterError("Repository identity mismatch")

        return RepositorySnapshot(
            owner=owner,
            name=name,
            default_branch=str(data.get("default_branch", "")),
            private=bool(data.get("private", True)),
            source="github_live",
        )

    def branch(self, repository: str, name: str) -> BranchSnapshot:
        owner, repo = self._check_repository(repository)
        encoded = quote(name, safe="")
        data = self._get(f"/repos/{owner}/{repo}/branches/{encoded}")

        if not isinstance(data, dict):
            raise AdapterError("Malformed branch response")

        sha = data.get("commit", {}).get("sha", "")
        if data.get("name") != name:
            raise AdapterError("Branch identity mismatch")

        return BranchSnapshot(name=name, sha=sha, source="github_live")

    def commit(self, repository: str, sha: str) -> CommitSnapshot:
        owner, repo = self._check_repository(repository)
        data = self._get(f"/repos/{owner}/{repo}/commits/{sha}")

        if not isinstance(data, dict):
            raise AdapterError("Malformed commit response")

        returned = data.get("sha", "")
        if returned != sha:
            raise AdapterError("Commit identity mismatch")

        message = data.get("commit", {}).get("message", "")
        # Bound the message; do not include author email or other PII.
        message = str(message).split("\n", 1)[0][:200]

        return CommitSnapshot(sha=sha, message=message, source="github_live")

    def pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        owner, repo = self._check_repository(repository)
        data = self._get(f"/repos/{owner}/{repo}/pulls/{number}")

        if not isinstance(data, dict):
            raise AdapterError("Malformed pull request response")

        head = data.get("head", {})
        base = data.get("base", {})

        # Reject forks whose repository identity cannot be established.
        head_repo = head.get("repo") or {}
        if head_repo.get("full_name") not in {None, repository}:
            raise AdapterError("Unsupported fork pull request")

        state = data.get("state")
        merged = bool(data.get("merged"))
        if merged:
            normalized = "merged"
        elif state == "open":
            normalized = "open"
        elif state == "closed":
            normalized = "closed"
        else:
            raise AdapterError("Invalid pull request state")

        return PullRequestSnapshot(
            number=number,
            title=str(data.get("title", ""))[:200],
            state=normalized,
            head_sha=str(head.get("sha", "")),
            head_branch=str(head.get("ref", "")),
            base_branch=str(base.get("ref", "")),
            source="github_live",
        )

    def _runs_for_sha(self, repository: str, sha: str) -> list[dict]:
        owner, repo = self._check_repository(repository)
        data = self._get(
            f"/repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=5"
        )
        if not isinstance(data, dict):
            return []
        runs = data.get("workflow_runs", [])
        if not isinstance(runs, list):
            return []
        return [run for run in runs if isinstance(run, dict)]

    def _status_from_run(self, run: dict, sha: str) -> CIStatus:
        # Only trust a run whose head_sha matches exactly.
        if run.get("head_sha") != sha:
            return CIStatus.UNAVAILABLE

        status = run.get("status")
        conclusion = run.get("conclusion")

        if status in {"queued", "waiting", "pending"}:
            return CIStatus.PENDING
        if status == "in_progress":
            return CIStatus.RUNNING
        if status == "completed":
            if conclusion == "success":
                return CIStatus.SUCCESS
            if conclusion == "failure":
                return CIStatus.FAILED
            if conclusion == "cancelled":
                return CIStatus.CANCELLED
            return CIStatus.UNAVAILABLE
        return CIStatus.UNAVAILABLE

    def workflow(self, repository: str, sha: str) -> WorkflowSnapshot:
        runs = self._runs_for_sha(repository, sha)
        matching = [run for run in runs if run.get("head_sha") == sha]

        if not matching:
            return WorkflowSnapshot(
                name="tests",
                status=CIStatus.UNAVAILABLE,
                head_sha=sha,
                source="github_live",
            )

        # Prefer the most recent matching run.
        run = matching[0]
        return WorkflowSnapshot(
            name=str(run.get("name", "tests")),
            status=self._status_from_run(run, sha),
            head_sha=sha,
            source="github_live",
        )

    def ci_result(self, repository: str, sha: str) -> CIResultSnapshot:
        workflow = self.workflow(repository, sha)

        # GitHub Actions does not provide independently verified pytest counts.
        # Leave counts as None rather than inventing them.
        return CIResultSnapshot(
            sha=sha,
            status=workflow.status,
            passed=None,
            failed=None,
            source="github_live",
        )
