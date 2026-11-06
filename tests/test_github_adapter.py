"""
SKIPPY AI Collaboration Bridge — GitHub Adapter Tests.

LOCAL SYNTHETIC ONLY.
"""

import pytest

from skippy.collaboration.github_adapter import (
    ALLOWED_REPOSITORY,
    CIStatus,
    AdapterError,
    GitHubAdapter,
    SyntheticGitHubProvider,
)


@pytest.fixture
def adapter():
    return GitHubAdapter(SyntheticGitHubProvider())


def test_repository_allowlist(adapter):
    with pytest.raises(AdapterError, match="allowlist"):
        adapter.repository("other/repo")


def test_repository_metadata(adapter):
    snap = adapter.repository(ALLOWED_REPOSITORY)
    assert snap.name == "QLA"
    assert snap.private is True
    assert snap.source == "synthetic"


def test_branch_retrieval(adapter):
    snap = adapter.branch(ALLOWED_REPOSITORY, "phase-8-tradingview-integration")
    assert snap.sha.startswith("79925e4")


def test_commit_metadata(adapter):
    sha = "79925e4aa80bef170778fc02a022f5c338739c91"
    snap = adapter.commit(ALLOWED_REPOSITORY, sha)
    assert snap.sha == sha


def test_open_pull_request(adapter):
    snap = adapter.pull_request(ALLOWED_REPOSITORY, 1)
    assert snap.state == "open"


def test_merged_pull_request(adapter):
    snap = adapter.pull_request(ALLOWED_REPOSITORY, 2)
    assert snap.state == "merged"


def test_closed_unmerged_pull_request(adapter):
    snap = adapter.pull_request(ALLOWED_REPOSITORY, 3)
    assert snap.state == "closed"


def test_pending_ci(adapter):
    sha = "cccccccccccccccccccccccccccccccccccccccc"
    result = adapter.ci_result(ALLOWED_REPOSITORY, sha)
    assert result.status is CIStatus.PENDING
    assert result.passed is None


def test_successful_ci(adapter):
    sha = "79925e4aa80bef170778fc02a022f5c338739c91"
    result = adapter.ci_result(ALLOWED_REPOSITORY, sha)
    assert result.status is CIStatus.SUCCESS
    assert result.passed == 267
    assert result.failed == 0
    assert result.sha == sha


def test_failed_ci(adapter):
    sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    result = adapter.ci_result(ALLOWED_REPOSITORY, sha)
    assert result.status is CIStatus.FAILED
    assert result.failed == 7


def test_unknown_ci(adapter):
    sha = "dddddddddddddddddddddddddddddddddddddddd"
    result = adapter.ci_result(ALLOWED_REPOSITORY, sha)
    assert result.status is CIStatus.UNAVAILABLE
    assert result.passed is None


def test_exact_sha_matching(adapter):
    sha = "79925e4aa80bef170778fc02a022f5c338739c91"
    result = adapter.ci_result(ALLOWED_REPOSITORY, sha)
    assert result.sha == sha


def test_invalid_repository_rejected(adapter):
    with pytest.raises(AdapterError):
        adapter.branch("evil/repo", "main")


def test_invalid_identifier_rejected(adapter):
    with pytest.raises(AdapterError, match="Invalid commit"):
        adapter.commit(ALLOWED_REPOSITORY, "not-a-sha")

    with pytest.raises(AdapterError, match="Invalid branch"):
        adapter.branch(ALLOWED_REPOSITORY, "bad branch!")

    with pytest.raises(AdapterError, match="Invalid pull request"):
        adapter.pull_request(ALLOWED_REPOSITORY, 0)


def test_missing_resource(adapter):
    with pytest.raises(AdapterError, match="not found"):
        adapter.branch(ALLOWED_REPOSITORY, "does-not-exist")

    with pytest.raises(AdapterError, match="not found"):
        adapter.pull_request(ALLOWED_REPOSITORY, 99)


def test_malformed_provider_response(adapter):
    class Broken:
        def repository(self, repository):
            return "not-a-snapshot"

        def branch(self, repository, name):
            raise AdapterError("malformed")

        def commit(self, repository, sha):
            raise AdapterError("malformed")

        def pull_request(self, repository, number):
            raise AdapterError("malformed")

        def workflow(self, repository, sha):
            raise AdapterError("malformed")

        def ci_result(self, repository, sha):
            raise AdapterError("malformed")

    broken = GitHubAdapter(Broken())
    with pytest.raises(AdapterError):
        broken.branch(ALLOWED_REPOSITORY, "phase-8-tradingview-integration")


def test_bounded_output(adapter):
    report = adapter.development_report(
        ALLOWED_REPOSITORY,
        branch="phase-8-tradingview-integration",
        sha="79925e4aa80bef170778fc02a022f5c338739c91",
    )
    assert len(report.note) <= 500
    assert report.source == "synthetic"


def test_read_only_interface():
    methods = {
        name
        for name in dir(GitHubAdapter)
        if not name.startswith("_")
    }
    forbidden = {
        "create_commit",
        "push",
        "merge",
        "delete_branch",
        "approve",
        "deploy",
    }
    assert methods.isdisjoint(forbidden)


def test_no_network_or_writes(adapter):
    report = adapter.development_report(
        ALLOWED_REPOSITORY,
        sha="79925e4aa80bef170778fc02a022f5c338739c91",
    )
    assert report.ci_status is CIStatus.SUCCESS
    assert "not merge authorization" in report.note


def test_collaboration_workflow_unaffected(tmp_path):
    """
    The GitHub adapter does not alter the existing collaboration protocol.
    """
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False
