"""
SKIPPY AI Collaboration Bridge — Authorization Policy Tests.

Synthetic evidence only. No network. No credentials. No execution.
"""

from datetime import datetime, timedelta, timezone

import pytest

from skippy.collaboration.authorization import (
    AuthorizationDecision,
    AuthorizationError,
    AuthorizationPolicy,
    AuthorizationRequest,
    AuthorizationState,
    OperationClass,
)


def _now():
    return datetime.now(timezone.utc)


def _request(**overrides):
    values = {
        "authorization_id": "auth-1",
        "chief_identity": "chief-ref",
        "requesting_participant": "chief",
        "action": "merge_pull_request",
        "action_digest": "digest-a",
        "repository": "SGGlobal/QLA",
        "project_id": "qla",
        "task_id": "task-1",
        "expected_sha": "abc1234",
        "environment": "local",
        "created_at": _now(),
        "expires_at": _now() + timedelta(hours=1),
        "state": AuthorizationState.APPROVED,
    }
    values.update(overrides)
    return AuthorizationRequest(**values)


def test_default_denial(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    decision = policy.decide(
        "missing",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert decision.allowed is False
    assert decision.execution_enabled is False


def test_valid_policy_classification(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    assert policy.classify("read_repository") is OperationClass.READ_ONLY
    assert policy.classify("merge_pull_request") is OperationClass.PROTECTED


def test_unknown_action_rejected(tmp_path):
    with pytest.raises(AuthorizationError, match="Unknown action"):
        _request(action="explode")


def test_unauthorized_participant_rejected(tmp_path):
    with pytest.raises(AuthorizationError, match="Unknown requesting"):
        _request(requesting_participant="hermes")


def test_forged_chief_identity(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request(requesting_participant="jason", chief_identity="jason"))

    decision = policy.decide(
        "auth-1",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert decision.reason == "AI impersonation of Chief"


def test_ai_cannot_submit_approved(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    with pytest.raises(AuthorizationError, match="manufacture"):
        policy.submit(
            _request(
                requesting_participant="skyppy",
                state=AuthorizationState.APPROVED,
            )
        )


def test_expiration(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request(expires_at=_now() - timedelta(seconds=1)))

    decision = policy.decide(
        "auth-1",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert decision.state is AuthorizationState.EXPIRED


def test_revocation(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())
    policy.revoke("auth-1")

    decision = policy.decide(
        "auth-1",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert decision.state is AuthorizationState.REVOKED


def test_one_time_consumption(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    first = policy.consume("auth-1")
    second = policy.consume("auth-1")

    assert first.state is AuthorizationState.CONSUMED
    assert first.execution_enabled is False
    assert second.allowed is False


def test_action_mismatch(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    decision = policy.decide(
        "auth-1",
        action="deploy",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert decision.reason == "Incorrect action"


def test_sha_mismatch(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    decision = policy.decide(
        "auth-1",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="different",
    )
    assert decision.reason == "Commit SHA does not match"


def test_repository_mismatch(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    decision = policy.decide(
        "auth-1",
        action="merge_pull_request",
        repository="other/repo",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert decision.reason == "Incorrect repository"


def test_halt_precedence(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())
    policy.halt()

    decision = policy.decide(
        "auth-1",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert decision.reason == "System is halted"

    consumed = policy.consume("auth-1")
    assert consumed.allowed is False


def test_sqlite_persistence(tmp_path):
    path = tmp_path / "auth.db"
    policy = AuthorizationPolicy(path)
    policy.submit(_request())

    reopened = AuthorizationPolicy(path)
    decision = reopened.decide(
        "auth-1",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert decision.state is AuthorizationState.APPROVED
    assert decision.execution_enabled is False


def test_no_execution_capability(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    decision = policy.decide(
        "auth-1",
        action="merge_pull_request",
        repository="SGGlobal/QLA",
        project_id="qla",
        task_id="task-1",
        action_digest="digest-a",
        expected_sha="abc1234",
    )
    assert isinstance(decision, AuthorizationDecision)
    assert decision.execution_enabled is False


def test_no_network_or_trading(tmp_path):
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False
