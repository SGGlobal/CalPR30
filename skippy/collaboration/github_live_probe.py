"""
SKIPPY AI Collaboration Bridge — Manual Live Verification Probe.

Default execution is offline.

This module must not be imported in a way that performs network access.
It must not run during tests, package import, application startup, or
scheduled tasks. No autonomous agent may invoke it.

Actual live requests are reserved for Step 69D.2B.2 after Chief's
separate authorization.
"""

from __future__ import annotations

import getpass
from typing import Callable

from skippy.collaboration.github_adapter import (
    ALLOWED_REPOSITORY,
    GitHubAdapter,
)
from skippy.collaboration.github_http_transport import production_transport
from skippy.collaboration.github_live_provider import (
    GitHubLiveProvider,
    CredentialSafeError,
)


class ProbeCancelled(RuntimeError):
    """Raised when the operator declines the live confirmation."""


def _require_confirmation(prompt: Callable[[str], str]) -> None:
    answer = prompt(
        "Type LIVE to confirm a read-only GitHub request to SGGlobal/QLA: "
    )
    if answer.strip() != "LIVE":
        raise ProbeCancelled("Live verification cancelled")


def _read_token(reader: Callable[[str], str]) -> str:
    token = reader("GitHub token (input hidden): ")
    if not isinstance(token, str) or not token.strip():
        raise CredentialSafeError("Missing GitHub token")
    return token.strip()


def run_probe(
    *,
    live: bool = False,
    confirmed: bool = False,
    token_reader: Callable[[str], str] | None = None,
    confirmer: Callable[[str], str] | None = None,
    pull_request: int | None = None,
) -> dict:
    """
    Manually gated, read-only verification.

    live and confirmed must both be True. The token is read interactively
    and never stored. Returns a report labeled source=github_live.
    """

    if not live:
        return {
            "status": "offline",
            "source": "synthetic",
            "note": "Default offline. No network request performed.",
        }

    if not confirmed:
        confirmer = confirmer or input
        _require_confirmation(confirmer)

    reader = token_reader or getpass.getpass
    token = _read_token(reader)

    transport = production_transport()
    provider = GitHubLiveProvider(token, transport)
    adapter = GitHubAdapter(provider, expected_source="github_live")

    repository = adapter.repository(ALLOWED_REPOSITORY)
    branch = adapter.branch(
        ALLOWED_REPOSITORY, "phase-8-tradingview-integration"
    )
    commit = adapter.commit(ALLOWED_REPOSITORY, branch.sha)

    pr = None
    if pull_request is not None:
        pr = adapter.pull_request(ALLOWED_REPOSITORY, pull_request)

    workflow = adapter.workflow(ALLOWED_REPOSITORY, branch.sha)

    return {
        "status": "completed",
        "source": "github_live",
        "repository": repository.name,
        "private": repository.private,
        "branch": branch.name,
        "sha": commit.sha,
        "pull_request": pr.number if pr else None,
        "ci_status": workflow.status.value,
        "note": (
            "Live GitHub observation. CI success is not merge authorization."
        ),
    }


if __name__ == "__main__":
    # Explicit invocation only. Not imported by tests or startup.
    print("This command requires Chief's separate authorization.")
    print("Use run_probe(live=True) from an authorized session.")
