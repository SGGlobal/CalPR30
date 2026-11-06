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

# Only this workflow name counts as the required QLA test suite.
REQUIRED_WORKFLOW_NAME = "tests"


class TransportError(AdapterError):
    """Raised when the HTTP transport cannot complete a request."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: dict[str, str]


HTTPTransport = Callable[[str, str, dict[str, str], int], HTTPResponse]


class CredentialSafeError(TransportError):
    """
    Transport error that never includes credential material.

    The original exception is not chained, so a token that appeared in a
    lower-level message cannot surface in a traceback or log.
    """

    def __init__(self, message: str):
        super().__init__(message)


def _redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "[REDACTED]")


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

    def _get(self, path: str) -> dict | list:
        if not path.startswith("/"):
            raise AdapterError("Invalid API path")

        url = f"{GITHUB_API_HOST}{path}"
        if not url.startswith(GITHUB_API_HOST):
            raise AdapterError("Unexpected API host")

        try:
            response = self._transport(
                "GET", url, self._headers(), REQUEST_TIMEOUT_SECONDS
            )
        except TimeoutError:
            raise CredentialSafeError("GitHub request timed out")
        except Exception as exc:
            # Do not chain; the original message may contain the token.
            safe = _redact(str(exc), self._token)
            raise CredentialSafeError(
                f"GitHub request failed: {safe}"
            ) from None

        if response.status in {301, 302, 303, 307, 308}:
            raise CredentialSafeError("Unexpected redirect")

        if response.status == 401:
            raise CredentialSafeError("Authentication failed")
        if response.status == 403:
            raise CredentialSafeError("Insufficient permissions or forbidden")
        if response.status == 404:
            raise AdapterError("Resource not found")
        if response.status == 429:
            raise CredentialSafeError("GitHub rate limit exceeded")
        if response.status >= 500:
            raise CredentialSafeError("GitHub server error")
        if response.status != 200:
            raise CredentialSafeError(
                f"Unexpected GitHub status {response.status}"
            )

        if len(response.body) > MAX_RESPONSE_BYTES:
            raise CredentialSafeError("Response exceeds size bound")

        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CredentialSafeError("Malformed JSON")

        if not isinstance(payload, (dict, list)):
            raise CredentialSafeError("Unexpected response type")

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
        if not isinstance(full, str) or full != repository:
            raise AdapterError("Repository identity mismatch")

        default_branch = data.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise AdapterError("Repository default branch is missing")

        if "private" not in data or not isinstance(data.get("private"), bool):
            raise AdapterError("Repository visibility is missing")

        return RepositorySnapshot(
            owner=owner,
            name=name,
            default_branch=default_branch,
            private=data["private"],
            source="github_live",
        )

    def branch(self, repository: str, name: str) -> BranchSnapshot:
        owner, repo = self._check_repository(repository)
        encoded = quote(name, safe="")
        data = self._get(f"/repos/{owner}/{repo}/branches/{encoded}")

        if not isinstance(data, dict):
            raise AdapterError("Malformed branch response")

        if data.get("name") != name:
            raise AdapterError("Branch identity mismatch")

        commit = data.get("commit")
        if not isinstance(commit, dict):
            raise AdapterError("Branch commit is missing")

        sha = commit.get("sha")
        if not isinstance(sha, str) or not sha:
            raise AdapterError("Branch HEAD SHA is missing")

        return BranchSnapshot(name=name, sha=sha, source="github_live")

    def commit(self, repository: str, sha: str) -> CommitSnapshot:
        owner, repo = self._check_repository(repository)
        data = self._get(f"/repos/{owner}/{repo}/commits/{sha}")

        if not isinstance(data, dict):
            raise AdapterError("Malformed commit response")

        returned = data.get("sha")
        if not isinstance(returned, str) or returned != sha:
            raise AdapterError("Commit identity mismatch")

        commit = data.get("commit")
        if not isinstance(commit, dict):
            raise AdapterError("Commit payload is missing")

        message = commit.get("message", "")
        message = str(message).split("\n", 1)[0][:200]

        return CommitSnapshot(sha=sha, message=message, source="github_live")

    def pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        owner, repo = self._check_repository(repository)
        data = self._get(f"/repos/{owner}/{repo}/pulls/{number}")

        if not isinstance(data, dict):
            raise AdapterError("Malformed pull request response")

        head = data.get("head")
        base = data.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise AdapterError("Pull request source information is missing")

        head_sha = head.get("sha")
        head_branch = head.get("ref")
        base_branch = base.get("ref")

        if not isinstance(head_sha, str) or not head_sha:
            raise AdapterError("Pull request HEAD SHA is missing")
        if not isinstance(head_branch, str) or not head_branch:
            raise AdapterError("Pull request source branch is missing")
        if not isinstance(base_branch, str) or not base_branch:
            raise AdapterError("Pull request base branch is missing")

        head_repo = head.get("repo") or {}
        if not isinstance(head_repo, dict):
            raise AdapterError("Pull request source repository is malformed")

        head_full = head_repo.get("full_name")
        if head_full not in {None, repository}:
            raise AdapterError("Unsupported fork pull request")

        state = data.get("state")
        merged = data.get("merged")
        if not isinstance(merged, bool):
            raise AdapterError("Pull request merge status is missing")

        if merged:
            normalized = "merged"
        elif state == "open":
            normalized = "open"
        elif state == "closed":
            normalized = "closed"
        else:
            raise AdapterError("Invalid pull request state")

        title = data.get("title", "")
        if not isinstance(title, str):
            title = ""

        return PullRequestSnapshot(
            number=number,
            title=title[:200],
            state=normalized,
            head_sha=head_sha,
            head_branch=head_branch,
            base_branch=base_branch,
            source="github_live",
        )

    def _runs_for_sha(self, repository: str, sha: str) -> list[dict]:
        owner, repo = self._check_repository(repository)
        data = self._get(
            f"/repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=10"
        )
        if not isinstance(data, dict):
            return []
        runs = data.get("workflow_runs")
        if not isinstance(runs, list):
            return []
        return [run for run in runs if isinstance(run, dict)]

    def _status_from_run(self, run: dict, sha: str) -> CIStatus:
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
        """
        Return the status of the required QLA test workflow only.

        Unrelated workflow success is ignored. If the required workflow
        is absent, the result is UNAVAILABLE, never SUCCESS.
        """

        runs = self._runs_for_sha(repository, sha)
        required = [
            run
            for run in runs
            if run.get("name") == REQUIRED_WORKFLOW_NAME
            and run.get("head_sha") == sha
        ]

        if not required:
            return WorkflowSnapshot(
                name=REQUIRED_WORKFLOW_NAME,
                status=CIStatus.UNAVAILABLE,
                head_sha=sha,
                source="github_live",
            )

        run = required[0]
        return WorkflowSnapshot(
            name=REQUIRED_WORKFLOW_NAME,
            status=self._status_from_run(run, sha),
            head_sha=sha,
            source="github_live",
        )

    def ci_result(self, repository: str, sha: str) -> CIResultSnapshot:
        workflow = self.workflow(repository, sha)

        # Counts remain None; GitHub does not provide verified pytest totals.
        return CIResultSnapshot(
            sha=sha,
            status=workflow.status,
            passed=None,
            failed=None,
            source="github_live",
        )
