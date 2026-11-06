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
        "created_at": _now() - timedelta(hours=2),
        "expires_at": _now() + timedelta(hours=1),
        "state": AuthorizationState.APPROVED,
    }
    values.update(overrides)
    return AuthorizationRequest(**values)


def _decide(policy, authorization_id="auth-1", **overrides):
    values = {
        "action": "merge_pull_request",
        "repository": "SGGlobal/QLA",
        "project_id": "qla",
        "task_id": "task-1",
        "action_digest": "digest-a",
        "expected_sha": "abc1234",
        "environment": "local",
    }
    values.update(overrides)
    return policy.decide(authorization_id, **values)


def test_default_denial(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    decision = _decide(policy, "missing")
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


def test_ai_approved_rejected_on_submit(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    with pytest.raises(AuthorizationError, match="manufacture"):
        policy.submit(
            _request(
                requesting_participant="jason",
                state=AuthorizationState.APPROVED,
            )
        )


def test_pending_impersonation_rejected_on_decide(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(
        _request(
            requesting_participant="jason",
            chief_identity="jason",
            state=AuthorizationState.PENDING,
        )
    )

    decision = _decide(policy)
    assert decision.reason == "AI impersonation of Chief"
    assert decision.execution_enabled is False


def test_expiration(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    created = _now() - timedelta(hours=2)
    policy.submit(
        _request(
            created_at=created,
            expires_at=created + timedelta(minutes=30),
        )
    )

    decision = _decide(policy)
    assert decision.state is AuthorizationState.EXPIRED


def test_expiration_blocks_consumption(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    created = _now() - timedelta(hours=2)
    policy.submit(
        _request(
            created_at=created,
            expires_at=created + timedelta(minutes=30),
        )
    )

    decision = policy.consume("auth-1")
    assert decision.state is AuthorizationState.EXPIRED
    assert decision.execution_enabled is False
    assert "Expired during consumption" in policy.events_for("auth-1")


def test_expires_before_created_rejected():
    created = _now()
    with pytest.raises(AuthorizationError, match="after created_at"):
        _request(
            created_at=created,
            expires_at=created - timedelta(seconds=1),
        )


def test_expires_equal_created_rejected():
    created = _now()
    with pytest.raises(AuthorizationError, match="after created_at"):
        _request(created_at=created, expires_at=created)


def test_revocation(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())
    policy.revoke("auth-1")

    decision = _decide(policy)
    assert decision.state is AuthorizationState.REVOKED


def test_consumed_is_terminal(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())
    policy.consume("auth-1")

    decision = policy.revoke("auth-1")
    assert decision.reason == "Terminal state cannot be overwritten"
    assert policy.stored_state("auth-1") == "CONSUMED"
    assert "Terminal state cannot be overwritten" in policy.events_for("auth-1")


def test_revoked_is_terminal(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())
    policy.revoke("auth-1")

    decision = policy.revoke("auth-1")
    assert decision.reason == "Terminal state cannot be overwritten"
    assert policy.stored_state("auth-1") == "REVOKED"


def test_expired_does_not_become_approved(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    created = _now() - timedelta(hours=2)
    policy.submit(
        _request(
            created_at=created,
            expires_at=created + timedelta(minutes=30),
        )
    )
    _decide(policy)

    assert policy.stored_state("auth-1") == "EXPIRED"
    second = _decide(policy)
    assert second.state is AuthorizationState.EXPIRED
    assert policy.stored_state("auth-1") == "EXPIRED"


def test_denied_does_not_become_approved(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request(state=AuthorizationState.DENIED))

    decision = _decide(policy)
    assert decision.state is AuthorizationState.DENIED
    assert policy.stored_state("auth-1") == "DENIED"


def test_revocation_blocks_consumption(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())
    policy.revoke("auth-1")

    decision = policy.consume("auth-1")
    assert decision.state is AuthorizationState.REVOKED
    assert "Authorization revoked" in policy.events_for("auth-1")


def test_denied_blocks_consumption(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request(state=AuthorizationState.DENIED))

    decision = policy.consume("auth-1")
    assert decision.state is AuthorizationState.DENIED
    assert "Authorization denied" in policy.events_for("auth-1")


def test_one_time_consumption(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    first = policy.consume("auth-1")
    second = policy.consume("auth-1")

    assert first.state is AuthorizationState.CONSUMED
    assert first.execution_enabled is False
    assert second.state is AuthorizationState.CONSUMED
    assert second.allowed is False
    assert "Authorization already consumed" in policy.events_for("auth-1")


def test_concurrent_consumption(tmp_path):
    path = tmp_path / "auth.db"
    policy = AuthorizationPolicy(path)
    policy.submit(_request())

    other = AuthorizationPolicy(path)
    first = policy.consume("auth-1")
    second = other.consume("auth-1")

    successes = [
        decision
        for decision in (first, second)
        if decision.state is AuthorizationState.CONSUMED
        and decision.reason == "Consumed; execution remains disabled"
    ]
    assert len(successes) == 1
    assert first.execution_enabled is False
    assert second.execution_enabled is False
    assert policy.stored_state("auth-1") == "CONSUMED"


def test_missing_authorization_audited(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    decision = policy.consume("missing")

    assert decision.reason == "Missing authorization"
    assert "Missing authorization" in policy.events_for("missing")


def test_invalid_state_audited(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request(state=AuthorizationState.PENDING))

    decision = policy.consume("auth-1")
    assert decision.reason == "Invalid state for consumption"
    assert "Invalid state for consumption" in policy.events_for("auth-1")


def test_action_mismatch(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    decision = _decide(policy, action="deploy")
    assert decision.reason == "Incorrect action"


def test_sha_mismatch(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    decision = _decide(policy, expected_sha="different")
    assert decision.reason == "Commit SHA does not match"


def test_repository_mismatch(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    decision = _decide(policy, repository="other/repo")
    assert decision.reason == "Incorrect repository"


def test_environment_mismatch(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request(environment="local"))

    decision = _decide(policy, environment="production")
    assert decision.reason == "Incorrect environment"
    assert decision.execution_enabled is False


def test_halt_precedence(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())
    policy.halt()

    decision = _decide(policy)
    assert decision.reason == "System is halted"

    consumed = policy.consume("auth-1")
    assert consumed.allowed is False
    assert consumed.reason == "System is halted"
    assert "System is halted" in policy.events_for("auth-1")


def test_halt_persists(tmp_path):
    path = tmp_path / "auth.db"
    policy = AuthorizationPolicy(path)
    policy.halt()

    reopened = AuthorizationPolicy(path)
    assert reopened.is_halted() is True


def test_protocol_halt_is_independent(tmp_path):
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    protocol.halt()

    assert protocol.is_halted is True
    assert policy.is_halted() is False

    decision = _decide(policy)
    assert decision.state is AuthorizationState.APPROVED
    assert decision.execution_enabled is False


def test_sqlite_persistence(tmp_path):
    path = tmp_path / "auth.db"
    policy = AuthorizationPolicy(path)
    policy.submit(_request())

    reopened = AuthorizationPolicy(path)
    decision = _decide(reopened)
    assert decision.state is AuthorizationState.APPROVED
    assert decision.execution_enabled is False


def test_no_execution_capability(tmp_path):
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    policy.submit(_request())

    decision = _decide(policy)
    assert isinstance(decision, AuthorizationDecision)
    assert decision.execution_enabled is False


def test_no_network_or_trading(tmp_path):
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False
