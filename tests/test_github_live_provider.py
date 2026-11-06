"""
SKIPPY AI Collaboration Bridge — Live Provider Tests.

Mocked HTTP only. No real credentials. No network.
"""

import json
import traceback

import pytest

from skippy.collaboration.github_adapter import (
    ALLOWED_REPOSITORY,
    CIStatus,
    AdapterError,
    GitHubAdapter,
    SyntheticGitHubProvider,
)
from skippy.collaboration.github_live_provider import (
    HTTPResponse,
    GitHubLiveProvider,
    TransportError,
)


SHA = "79925e4aa80bef170778fc02a022f5c338739c91"
TOKEN = "ghp_test_token_not_real"
WORKFLOW_PATH = ".github/workflows/test.yml"


def json_response(payload, status=200):
    return HTTPResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers={},
    )


def make_transport(routes):
    calls = []

    def transport(method, url, headers, timeout):
        calls.append((method, url))
        for prefix, response in routes:
            if url.startswith(prefix) or prefix in url:
                if callable(response):
                    return response(method, url, headers, timeout)
                return response
        raise TransportError("unmapped")

    transport.calls = calls
    return transport


def _pr_payload(number, state, merged, ref):
    return {
        "number": number,
        "title": state.title(),
        "state": state,
        "merged": merged,
        "head": {
            "sha": SHA,
            "ref": ref,
            "repo": {"full_name": "SGGlobal/QLA"},
        },
        "base": {"ref": "phase-8-tradingview-integration"},
    }


def test_repository_metadata():
    transport = make_transport([
        ("/repos/SGGlobal/QLA",
         json_response({
             "full_name": "SGGlobal/QLA",
             "default_branch": "phase-8-tradingview-integration",
             "private": True,
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.repository(ALLOWED_REPOSITORY)

    assert snap.name == "QLA"
    assert snap.source == "github_live"
    assert transport.calls[0][0] == "GET"


def test_branch_retrieval():
    transport = make_transport([
        ("/branches/",
         json_response({
             "name": "phase-8-tradingview-integration",
             "commit": {"sha": SHA},
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.branch(ALLOWED_REPOSITORY, "phase-8-tradingview-integration")

    assert snap.sha == SHA
    assert snap.source == "github_live"


def test_commit_retrieval():
    transport = make_transport([
        ("/commits/",
         json_response({
             "sha": SHA,
             "commit": {"message": "checkpoint\nbody"},
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.commit(ALLOWED_REPOSITORY, SHA)

    assert snap.sha == SHA
    assert snap.message == "checkpoint"


def test_open_pull_request():
    transport = make_transport([
        ("/pulls/1", json_response(_pr_payload(1, "open", False, "feature/x"))),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.pull_request(ALLOWED_REPOSITORY, 1)

    assert snap.state == "open"
    assert snap.head_branch == "feature/x"
    assert snap.base_branch == "phase-8-tradingview-integration"


def test_merged_pull_request():
    transport = make_transport([
        ("/pulls/2", json_response(_pr_payload(2, "closed", True, "feature/y"))),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.pull_request(ALLOWED_REPOSITORY, 2)

    assert snap.state == "merged"


def test_closed_unmerged_pull_request():
    transport = make_transport([
        ("/pulls/3", json_response(_pr_payload(3, "closed", False, "feature/z"))),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.pull_request(ALLOWED_REPOSITORY, 3)

    assert snap.state == "closed"


def test_valid_source_repository():
    transport = make_transport([
        ("/pulls/1", json_response(_pr_payload(1, "open", False, "feature/x"))),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.pull_request(ALLOWED_REPOSITORY, 1)

    assert snap.head_branch == "feature/x"


def test_missing_source_repository_rejected():
    payload = _pr_payload(1, "open", False, "feature/x")
    payload["head"]["repo"] = None

    transport = make_transport([
        ("/pulls/1", json_response(payload)),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(AdapterError, match="source repository"):
        provider.pull_request(ALLOWED_REPOSITORY, 1)


def test_incorrect_source_repository_rejected():
    payload = _pr_payload(1, "open", False, "feature/x")
    payload["head"]["repo"] = {"full_name": "other/fork"}

    transport = make_transport([
        ("/pulls/1", json_response(payload)),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(AdapterError, match="Unsupported fork"):
        provider.pull_request(ALLOWED_REPOSITORY, 1)


def test_pr_number_mismatch_rejected():
    payload = _pr_payload(99, "open", False, "feature/x")

    transport = make_transport([
        ("/pulls/1", json_response(payload)),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(AdapterError, match="number mismatch"):
        provider.pull_request(ALLOWED_REPOSITORY, 1)


def test_required_workflow_by_path():
    transport = make_transport([
        ("/actions/runs",
         json_response({
             "workflow_runs": [{
                 "id": 1,
                 "name": "Test",
                 "path": WORKFLOW_PATH,
                 "status": "completed",
                 "conclusion": "success",
                 "head_sha": SHA,
             }],
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.workflow(ALLOWED_REPOSITORY, SHA)

    assert snap.name == "Test"
    assert snap.status is CIStatus.SUCCESS


def test_unrelated_workflow_success_is_not_required():
    transport = make_transport([
        ("/actions/runs",
         json_response({
             "workflow_runs": [{
                 "id": 2,
                 "name": "Deploy",
                 "path": ".github/workflows/deploy.yml",
                 "status": "completed",
                 "conclusion": "success",
                 "head_sha": SHA,
             }],
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.workflow(ALLOWED_REPOSITORY, SHA)

    assert snap.status is CIStatus.UNAVAILABLE


def test_multiple_matching_runs_unavailable():
    transport = make_transport([
        ("/actions/runs",
         json_response({
             "workflow_runs": [
                 {
                     "id": 10,
                     "name": "Test",
                     "path": WORKFLOW_PATH,
                     "status": "completed",
                     "conclusion": "success",
                     "head_sha": SHA,
                 },
                 {
                     "id": 11,
                     "name": "Test",
                     "path": WORKFLOW_PATH,
                     "status": "completed",
                     "conclusion": "failure",
                     "head_sha": SHA,
                 },
             ],
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.workflow(ALLOWED_REPOSITORY, SHA)

    assert snap.status is CIStatus.UNAVAILABLE


def test_pending_workflow():
    transport = make_transport([
        ("/actions/runs",
         json_response({
             "workflow_runs": [{
                 "id": 3,
                 "name": "Test",
                 "path": WORKFLOW_PATH,
                 "status": "queued",
                 "conclusion": None,
                 "head_sha": SHA,
             }],
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.workflow(ALLOWED_REPOSITORY, SHA)

    assert snap.status is CIStatus.PENDING


def test_successful_workflow():
    transport = make_transport([
        ("/actions/runs",
         json_response({
             "workflow_runs": [{
                 "id": 4,
                 "name": "Test",
                 "path": WORKFLOW_PATH,
                 "status": "completed",
                 "conclusion": "success",
                 "head_sha": SHA,
             }],
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    result = provider.ci_result(ALLOWED_REPOSITORY, SHA)

    assert result.status is CIStatus.SUCCESS
    assert result.passed is None
    assert result.failed is None


def test_failed_workflow():
    transport = make_transport([
        ("/actions/runs",
         json_response({
             "workflow_runs": [{
                 "id": 5,
                 "name": "Test",
                 "path": WORKFLOW_PATH,
                 "status": "completed",
                 "conclusion": "failure",
                 "head_sha": SHA,
             }],
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.workflow(ALLOWED_REPOSITORY, SHA)

    assert snap.status is CIStatus.FAILED


def test_missing_workflow():
    transport = make_transport([
        ("/actions/runs", json_response({"workflow_runs": []})),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.workflow(ALLOWED_REPOSITORY, SHA)

    assert snap.status is CIStatus.UNAVAILABLE


def test_sha_mismatch_is_unavailable():
    other = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    transport = make_transport([
        ("/actions/runs",
         json_response({
             "workflow_runs": [{
                 "id": 6,
                 "name": "Test",
                 "path": WORKFLOW_PATH,
                 "status": "completed",
                 "conclusion": "success",
                 "head_sha": other,
             }],
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    snap = provider.workflow(ALLOWED_REPOSITORY, SHA)

    assert snap.status is CIStatus.UNAVAILABLE


def test_authentication_failure():
    transport = make_transport([
        ("/repos/", HTTPResponse(401, b"", {})),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(TransportError, match="Authentication failed"):
        provider.repository(ALLOWED_REPOSITORY)


def test_missing_token():
    with pytest.raises(AdapterError, match="Missing GitHub token"):
        GitHubLiveProvider("", make_transport([]))


def test_insufficient_permissions():
    transport = make_transport([
        ("/repos/", HTTPResponse(403, b"", {})),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(TransportError, match="Insufficient permissions"):
        provider.repository(ALLOWED_REPOSITORY)


def test_rate_limiting():
    transport = make_transport([
        ("/repos/", HTTPResponse(429, b"", {})),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(TransportError, match="rate limit"):
        provider.repository(ALLOWED_REPOSITORY)


def test_timeout_does_not_leak_token():
    def boom(method, url, headers, timeout):
        raise TimeoutError(f"slow with {TOKEN}")

    transport = make_transport([("/repos/", boom)])
    provider = GitHubLiveProvider(TOKEN, transport)

    try:
        provider.repository(ALLOWED_REPOSITORY)
    except TransportError as exc:
        rendered = "".join(traceback.format_exception(exc))
        assert TOKEN not in str(exc)
        assert TOKEN not in rendered
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected TransportError")


def test_unexpected_exception_does_not_leak_token():
    def leaky(method, url, headers, timeout):
        raise RuntimeError(f"failed with token {TOKEN}")

    transport = make_transport([("/repos/", leaky)])
    provider = GitHubLiveProvider(TOKEN, transport)

    try:
        provider.repository(ALLOWED_REPOSITORY)
    except TransportError as exc:
        rendered = "".join(traceback.format_exception(exc))
        assert TOKEN not in str(exc)
        assert TOKEN not in rendered
        assert str(exc) == "GitHub request failed"
    else:
        raise AssertionError("expected TransportError")


def test_malformed_json():
    transport = make_transport([
        ("/repos/", HTTPResponse(200, b"not-json", {})),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(TransportError, match="Malformed JSON"):
        provider.repository(ALLOWED_REPOSITORY)


def test_oversized_response():
    transport = make_transport([
        ("/repos/", HTTPResponse(200, b"x" * 2_000_000, {})),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(TransportError, match="size bound"):
        provider.repository(ALLOWED_REPOSITORY)


def test_unauthorized_repository():
    provider = GitHubLiveProvider(TOKEN, make_transport([]))

    with pytest.raises(AdapterError, match="allowlist"):
        provider.repository("other/repo")


def test_unexpected_redirect():
    transport = make_transport([
        ("/repos/", HTTPResponse(302, b"", {})),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(TransportError, match="redirect"):
        provider.repository(ALLOWED_REPOSITORY)


def test_malformed_repository_identity():
    transport = make_transport([
        ("/repos/SGGlobal/QLA",
         json_response({
             "full_name": "other/repo",
             "default_branch": "main",
             "private": True,
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(AdapterError, match="identity mismatch"):
        provider.repository(ALLOWED_REPOSITORY)


def test_missing_repository_fields():
    transport = make_transport([
        ("/repos/SGGlobal/QLA",
         json_response({"full_name": "SGGlobal/QLA"})),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(AdapterError, match="default branch"):
        provider.repository(ALLOWED_REPOSITORY)


def test_missing_pull_request_source():
    transport = make_transport([
        ("/pulls/1",
         json_response({
             "number": 1,
             "title": "Open",
             "state": "open",
             "merged": False,
             "head": {},
             "base": {"ref": "phase-8-tradingview-integration"},
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)

    with pytest.raises(AdapterError, match="source"):
        provider.pull_request(ALLOWED_REPOSITORY, 1)


def test_invalid_provenance_rejected():
    transport = make_transport([
        ("/repos/SGGlobal/QLA",
         json_response({
             "full_name": "SGGlobal/QLA",
             "default_branch": "phase-8-tradingview-integration",
             "private": True,
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    adapter = GitHubAdapter(provider, expected_source="synthetic")

    with pytest.raises(AdapterError, match="Provenance mismatch"):
        adapter.repository(ALLOWED_REPOSITORY)


def test_default_synthetic_mode():
    adapter = GitHubAdapter(SyntheticGitHubProvider())
    snap = adapter.repository(ALLOWED_REPOSITORY)
    assert snap.source == "synthetic"


def test_explicit_authenticated_mode():
    transport = make_transport([
        ("/repos/SGGlobal/QLA",
         json_response({
             "full_name": "SGGlobal/QLA",
             "default_branch": "phase-8-tradingview-integration",
             "private": True,
         })),
    ])
    provider = GitHubLiveProvider(TOKEN, transport)
    adapter = GitHubAdapter(provider, expected_source="github_live")
    snap = adapter.repository(ALLOWED_REPOSITORY)

    assert snap.source == "github_live"


def test_no_network_by_default():
    adapter = GitHubAdapter(SyntheticGitHubProvider())
    report = adapter.development_report(ALLOWED_REPOSITORY)
    assert report.source == "synthetic"


def test_no_credential_leakage():
    try:
        GitHubLiveProvider("", make_transport([]))
    except AdapterError as exc:
        assert TOKEN not in str(exc)
        assert "ghp_" not in str(exc)


def test_no_write_operations():
    methods = {
        name
        for name in dir(GitHubLiveProvider)
        if not name.startswith("_")
    }
    forbidden = {"create_commit", "push", "merge", "delete_branch", "approve"}
    assert methods.isdisjoint(forbidden)


def test_no_trading_side_effects(tmp_path):
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False
