"""
SKIPPY AI Collaboration Bridge — Offline GitHub Adapter.

LOCAL SYNTHETIC ONLY.
No live GitHub API, no credentials, no repository writes, no trading.

This module defines the read-only contract and a deterministic simulated
provider. A future authenticated provider may implement the same contract.
Simulated observations must never be presented as live GitHub results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


ALLOWED_REPOSITORY = "SGGlobal/QLA"
MAX_SUMMARY_LENGTH = 500

BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


class AdapterError(ValueError):
    """Raised when an adapter request is invalid or cannot be completed."""


class CIStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RepositorySnapshot:
    owner: str
    name: str
    default_branch: str
    private: bool
    source: str = "synthetic"


@dataclass(frozen=True)
class BranchSnapshot:
    name: str
    sha: str
    source: str = "synthetic"


@dataclass(frozen=True)
class CommitSnapshot:
    sha: str
    message: str
    source: str = "synthetic"


@dataclass(frozen=True)
class PullRequestSnapshot:
    number: int
    title: str
    state: str  # open, closed, merged
    head_sha: str
    base_branch: str
    source: str = "synthetic"


@dataclass(frozen=True)
class WorkflowSnapshot:
    name: str
    status: CIStatus
    head_sha: str
    source: str = "synthetic"


@dataclass(frozen=True)
class CIResultSnapshot:
    sha: str
    status: CIStatus
    passed: int | None
    failed: int | None
    source: str = "synthetic"

    def __post_init__(self):
        if self.status is CIStatus.SUCCESS and (
            self.passed is None or self.failed is None
        ):
            # Success with unknown counts is allowed; counts stay None.
            return


@dataclass(frozen=True)
class DevelopmentReport:
    repository: str
    branch: str | None
    sha: str | None
    pull_request: int | None
    ci_status: CIStatus
    ci_passed: int | None
    ci_failed: int | None
    note: str
    source: str = "synthetic"


class GitHubProvider(Protocol):
    """Read-only provider contract. No write methods."""

    def repository(self, repository: str) -> RepositorySnapshot: ...
    def branch(self, repository: str, name: str) -> BranchSnapshot: ...
    def commit(self, repository: str, sha: str) -> CommitSnapshot: ...
    def pull_request(self, repository: str, number: int) -> PullRequestSnapshot: ...
    def workflow(self, repository: str, sha: str) -> WorkflowSnapshot: ...
    def ci_result(self, repository: str, sha: str) -> CIResultSnapshot: ...


class GitHubAdapter:
    """
    Read-only adapter over a provider.

    Enforces the repository allowlist, input validation, and fail-closed
    behavior. Does not perform network calls or Git operations.
    """

    def __init__(self, provider: GitHubProvider):
        self._provider = provider

    def _check_repository(self, repository: str) -> None:
        if not isinstance(repository, str) or repository != ALLOWED_REPOSITORY:
            raise AdapterError("Repository is not on the allowlist")

    def _check_branch(self, name: str) -> None:
        if not isinstance(name, str) or not BRANCH_PATTERN.match(name):
            raise AdapterError("Invalid branch name")

    def _check_sha(self, sha: str) -> None:
        if not isinstance(sha, str) or not SHA_PATTERN.match(sha):
            raise AdapterError("Invalid commit identifier")

    def _check_pr(self, number: int) -> None:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise AdapterError("Invalid pull request number")

    def repository(self, repository: str) -> RepositorySnapshot:
        self._check_repository(repository)
        return self._provider.repository(repository)

    def branch(self, repository: str, name: str) -> BranchSnapshot:
        self._check_repository(repository)
        self._check_branch(name)
        return self._provider.branch(repository, name)

    def commit(self, repository: str, sha: str) -> CommitSnapshot:
        self._check_repository(repository)
        self._check_sha(sha)
        return self._provider.commit(repository, sha)

    def pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        self._check_repository(repository)
        self._check_pr(number)
        return self._provider.pull_request(repository, number)

    def workflow(self, repository: str, sha: str) -> WorkflowSnapshot:
        self._check_repository(repository)
        self._check_sha(sha)
        return self._provider.workflow(repository, sha)

    def ci_result(self, repository: str, sha: str) -> CIResultSnapshot:
        self._check_repository(repository)
        self._check_sha(sha)
        result = self._provider.ci_result(repository, sha)
        if result.sha != sha:
            raise AdapterError("CI result SHA does not match requested SHA")
        return result

    def development_report(
        self,
        repository: str,
        branch: str | None = None,
        sha: str | None = None,
        pull_request: int | None = None,
    ) -> DevelopmentReport:
        """
        Build a synthetic development report.

        CI success is evidence of testing, not permission to merge.
        """

        self._check_repository(repository)

        ci_status = CIStatus.UNAVAILABLE
        passed = None
        failed = None

        if sha is not None:
            result = self.ci_result(repository, sha)
            ci_status = result.status
            passed = result.passed
            failed = result.failed

        note = (
            "Synthetic observation. CI success is not merge authorization."
        )
        if len(note) > MAX_SUMMARY_LENGTH:
            raise AdapterError("Report exceeds size bound")

        return DevelopmentReport(
            repository=repository,
            branch=branch,
            sha=sha,
            pull_request=pull_request,
            ci_status=ci_status,
            ci_passed=passed,
            ci_failed=failed,
            note=note,
        )


class SyntheticGitHubProvider:
    """
    Deterministic offline provider.

    Fixtures are explicit. Observations are always marked source='synthetic'.
    Missing resources raise AdapterError. Unknown CI is UNAVAILABLE, never SUCCESS.
    """

    def __init__(self):
        self._repositories = {
            ALLOWED_REPOSITORY: RepositorySnapshot(
                owner="SGGlobal",
                name="QLA",
                default_branch="phase-8-tradingview-integration",
                private=True,
            ),
        }
        self._branches = {
            (ALLOWED_REPOSITORY, "phase-8-tradingview-integration"): BranchSnapshot(
                name="phase-8-tradingview-integration",
                sha="79925e4aa80bef170778fc02a022f5c338739c91",
            ),
            (ALLOWED_REPOSITORY, "feature/phase9-github-adapter"): BranchSnapshot(
                name="feature/phase9-github-adapter",
                sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ),
        }
        self._commits = {
            (ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"): CommitSnapshot(
                sha="79925e4aa80bef170778fc02a022f5c338739c91",
                message="Verified integration checkpoint",
            ),
            (ALLOWED_REPOSITORY, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"): CommitSnapshot(
                sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                message="Synthetic feature commit",
            ),
            (ALLOWED_REPOSITORY, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"): CommitSnapshot(
                sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                message="Synthetic failing commit",
            ),
            (ALLOWED_REPOSITORY, "cccccccccccccccccccccccccccccccccccccccc"): CommitSnapshot(
                sha="cccccccccccccccccccccccccccccccccccccccc",
                message="Synthetic pending commit",
            ),
        }
        self._pulls = {
            (ALLOWED_REPOSITORY, 1): PullRequestSnapshot(
                number=1, title="Open PR", state="open",
                head_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                base_branch="phase-8-tradingview-integration",
            ),
            (ALLOWED_REPOSITORY, 2): PullRequestSnapshot(
                number=2, title="Merged PR", state="merged",
                head_sha="79925e4aa80bef170778fc02a022f5c338739c91",
                base_branch="phase-8-tradingview-integration",
            ),
            (ALLOWED_REPOSITORY, 3): PullRequestSnapshot(
                number=3, title="Closed PR", state="closed",
                head_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                base_branch="phase-8-tradingview-integration",
            ),
        }
        self._ci = {
            (ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"): CIResultSnapshot(
                sha="79925e4aa80bef170778fc02a022f5c338739c91",
                status=CIStatus.SUCCESS,
                passed=267,
                failed=0,
            ),
            (ALLOWED_REPOSITORY, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"): CIResultSnapshot(
                sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                status=CIStatus.SUCCESS,
                passed=267,
                failed=0,
            ),
            (ALLOWED_REPOSITORY, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"): CIResultSnapshot(
                sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                status=CIStatus.FAILED,
                passed=260,
                failed=7,
            ),
            (ALLOWED_REPOSITORY, "cccccccccccccccccccccccccccccccccccccccc"): CIResultSnapshot(
                sha="cccccccccccccccccccccccccccccccccccccccc",
                status=CIStatus.PENDING,
                passed=None,
                failed=None,
            ),
        }

    def repository(self, repository: str) -> RepositorySnapshot:
        snap = self._repositories.get(repository)
        if snap is None:
            raise AdapterError("Repository not found")
        return snap

    def branch(self, repository: str, name: str) -> BranchSnapshot:
        snap = self._branches.get((repository, name))
        if snap is None:
            raise AdapterError("Branch not found")
        return snap

    def commit(self, repository: str, sha: str) -> CommitSnapshot:
        snap = self._commits.get((repository, sha))
        if snap is None:
            raise AdapterError("Commit not found")
        return snap

    def pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        snap = self._pulls.get((repository, number))
        if snap is None:
            raise AdapterError("Pull request not found")
        return snap

    def workflow(self, repository: str, sha: str) -> WorkflowSnapshot:
        result = self._ci.get((repository, sha))
        if result is None:
            return WorkflowSnapshot(
                name="tests",
                status=CIStatus.UNAVAILABLE,
                head_sha=sha,
            )
        return WorkflowSnapshot(
            name="tests",
            status=result.status,
            head_sha=sha,
        )

    def ci_result(self, repository: str, sha: str) -> CIResultSnapshot:
        result = self._ci.get((repository, sha))
        if result is None:
            return CIResultSnapshot(
                sha=sha,
                status=CIStatus.UNAVAILABLE,
                passed=None,
                failed=None,
            )
        return result
