"""
SKIPPY AI Collaboration Bridge — GitHub Adapter Tests.

LOCAL SYNTHETIC ONLY.
"""

import pytest

from skippy.collaboration.github_adapter import (
    ALLOWED_REPOSITORY,
    CIResultSnapshot,
    CIStatus,
    AdapterError,
    BranchSnapshot,
    CommitSnapshot,
    GitHubAdapter,
    PullRequestSnapshot,
    RepositorySnapshot,
    SyntheticGitHubProvider,
    WorkflowSnapshot,
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
    assert snap.head_branch == "feature/phase9-github-adapter"


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
        broken.repository(ALLOWED_REPOSITORY)


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
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False


def test_invalid_branch_in_report(adapter):
    with pytest.raises(AdapterError, match="Invalid branch"):
        adapter.development_report(ALLOWED_REPOSITORY, branch="bad branch!")


def test_invalid_pr_in_report(adapter):
    with pytest.raises(AdapterError, match="Invalid pull request"):
        adapter.development_report(ALLOWED_REPOSITORY, pull_request=0)


def test_nonexistent_branch_in_report(adapter):
    with pytest.raises(AdapterError, match="not found"):
        adapter.development_report(ALLOWED_REPOSITORY, branch="missing-branch")


def test_branch_head_mismatch(adapter):
    with pytest.raises(AdapterError, match="Branch HEAD does not match"):
        adapter.development_report(
            ALLOWED_REPOSITORY,
            branch="phase-8-tradingview-integration",
            sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


def test_pr_head_mismatch(adapter):
    with pytest.raises(AdapterError, match="Pull request HEAD does not match"):
        adapter.development_report(
            ALLOWED_REPOSITORY,
            pull_request=1,
            sha="79925e4aa80bef170778fc02a022f5c338739c91",
        )


def test_commit_only_historical_report(adapter):
    sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    report = adapter.development_report(ALLOWED_REPOSITORY, sha=sha)
    assert report.sha == sha
    assert report.branch is None
    assert report.ci_status is CIStatus.SUCCESS


def test_valid_branch_and_pr_association(adapter):
    report = adapter.development_report(
        ALLOWED_REPOSITORY,
        branch="feature/phase9-github-adapter",
        pull_request=1,
        sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    assert report.branch == "feature/phase9-github-adapter"
    assert report.pull_request == 1
    assert report.ci_status is CIStatus.SUCCESS


def test_unrelated_branch_and_pr_without_sha(adapter):
    with pytest.raises(AdapterError, match="not the pull request source"):
        adapter.development_report(
            ALLOWED_REPOSITORY,
            branch="phase-8-tradingview-integration",
            pull_request=1,
        )


def test_unrelated_branch_and_pr_same_sha(adapter):
    with pytest.raises(AdapterError, match="not the pull request source"):
        adapter.development_report(
            ALLOWED_REPOSITORY,
            branch="feature/phase9-github-adapter",
            pull_request=2,
        )


def test_malformed_branch_head_sha():
    class Bad:
        def branch(self, repository, name):
            return BranchSnapshot(name=name, sha="not-a-sha")

    with pytest.raises(AdapterError, match="Malformed branch HEAD SHA"):
        GitHubAdapter(Bad()).branch(
            ALLOWED_REPOSITORY, "phase-8-tradingview-integration"
        )


def test_malformed_pr_head_sha():
    class Bad:
        def pull_request(self, repository, number):
            return PullRequestSnapshot(
                number=number,
                title="bad",
                state="open",
                head_sha="not-a-sha",
                head_branch="feature/x",
                base_branch="main",
            )

    with pytest.raises(AdapterError, match="Malformed pull request HEAD SHA"):
        GitHubAdapter(Bad()).pull_request(ALLOWED_REPOSITORY, 1)


def test_existing_successful_report(adapter):
    report = adapter.development_report(
        ALLOWED_REPOSITORY,
        branch="phase-8-tradingview-integration",
        sha="79925e4aa80bef170778fc02a022f5c338739c91",
    )
    assert report.ci_status is CIStatus.SUCCESS
    assert report.ci_passed == 267


def test_malformed_repository_snapshot():
    class Bad:
        def repository(self, repository):
            return "nope"

    with pytest.raises(AdapterError, match="Malformed repository"):
        GitHubAdapter(Bad()).repository(ALLOWED_REPOSITORY)


def test_malformed_branch_snapshot():
    class Bad:
        def branch(self, repository, name):
            return "nope"

    with pytest.raises(AdapterError, match="Malformed branch"):
        GitHubAdapter(Bad()).branch(
            ALLOWED_REPOSITORY, "phase-8-tradingview-integration"
        )


def test_malformed_commit_snapshot():
    class Bad:
        def commit(self, repository, sha):
            return "nope"

    with pytest.raises(AdapterError, match="Malformed commit"):
        GitHubAdapter(Bad()).commit(
            ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"
        )


def test_malformed_pull_snapshot():
    class Bad:
        def pull_request(self, repository, number):
            return "nope"

    with pytest.raises(AdapterError, match="Malformed pull request"):
        GitHubAdapter(Bad()).pull_request(ALLOWED_REPOSITORY, 1)


def test_malformed_workflow_snapshot():
    class Bad:
        def workflow(self, repository, sha):
            return "nope"

    with pytest.raises(AdapterError, match="Malformed workflow"):
        GitHubAdapter(Bad()).workflow(
            ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"
        )


def test_malformed_ci_snapshot():
    class Bad:
        def ci_result(self, repository, sha):
            return "nope"

    with pytest.raises(AdapterError, match="Malformed CI"):
        GitHubAdapter(Bad()).ci_result(
            ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"
        )


def test_invalid_ci_status():
    class Bad:
        def ci_result(self, repository, sha):
            return CIResultSnapshot(
                sha=sha,
                status="SUCCESS",  # type: ignore[arg-type]
                passed=1,
                failed=0,
            )

    with pytest.raises(AdapterError, match="Invalid CI status"):
        GitHubAdapter(Bad()).ci_result(
            ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"
        )


def test_invalid_test_counts():
    class Bad:
        def ci_result(self, repository, sha):
            return CIResultSnapshot(
                sha=sha,
                status=CIStatus.SUCCESS,
                passed=-1,
                failed=0,
            )

    with pytest.raises(AdapterError, match="Invalid passed"):
        GitHubAdapter(Bad()).ci_result(
            ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"
        )


def test_success_with_failed_count_rejected():
    class Bad:
        def ci_result(self, repository, sha):
            return CIResultSnapshot(
                sha=sha,
                status=CIStatus.SUCCESS,
                passed=10,
                failed=1,
            )

    with pytest.raises(AdapterError, match="conceal"):
        GitHubAdapter(Bad()).ci_result(
            ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"
        )


def test_wrong_commit_sha_rejected():
    class Bad:
        def ci_result(self, repository, sha):
            return CIResultSnapshot(
                sha="ffffffffffffffffffffffffffffffffffffffff",
                status=CIStatus.SUCCESS,
                passed=1,
                failed=0,
            )

    with pytest.raises(AdapterError, match="does not match"):
        GitHubAdapter(Bad()).ci_result(
            ALLOWED_REPOSITORY, "79925e4aa80bef170778fc02a022f5c338739c91"
        )


def test_non_synthetic_source_rejected():
    """
    An unrecognized source is rejected as invalid provenance.
    The synthetic-mode adapter does not accept it as evidence.
    """
    class Bad:
        def repository(self, repository):
            return RepositorySnapshot(
                owner="SGGlobal",
                name="QLA",
                default_branch="phase-8-tradingview-integration",
                private=True,
                source="live",
            )

    with pytest.raises(AdapterError, match="Invalid provenance"):
        GitHubAdapter(Bad()).repository(ALLOWED_REPOSITORY)
