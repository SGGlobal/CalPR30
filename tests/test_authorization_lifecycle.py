"""
SKIPPY AI Collaboration Bridge — Authorization Lifecycle Tests.

Synthetic keys only. No network. No execution.
"""

import threading
from datetime import datetime, timedelta, timezone

from skippy.collaboration.authorization import (
    AuthorizationPolicy,
    AuthorizationRequest,
    AuthorizationState,
)
from skippy.collaboration.authorization_lifecycle import (
    AuthorizationLifecycle,
    LifecycleResult,
)
from skippy.collaboration.chief_identity import (
    ChiefIdentityVerifier,
    canonical_payload,
)
from skippy.collaboration.protocol import CollaborationProtocol


def _now():
    return datetime.now(timezone.utc)


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    private = Ed25519PrivateKey.generate()
    return private, private.public_key().public_bytes_raw()


def _setup(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    verifier.register_public_key("test-key", public)

    created = _now() - timedelta(minutes=1)
    policy.submit(
        AuthorizationRequest(
            authorization_id="auth-1",
            chief_identity="chief-ref",
            requesting_participant="chief",
            action="merge_pull_request",
            action_digest="digest-a",
            repository="SGGlobal/QLA",
            project_id="qla",
            task_id="task-1",
            expected_sha="a" * 40,
            environment="local",
            created_at=created,
            expires_at=created + timedelta(hours=1),
            state=AuthorizationState.PENDING,
        )
    )

    challenge = verifier.issue_challenge(
        authorization_id="auth-1",
        action="merge_pull_request",
        action_digest="digest-a",
        repository="SGGlobal/QLA",
        environment="local",
        project_id="qla",
        task_id="task-1",
        expected_sha="a" * 40,
        key_id="test-key",
    )

    bridge = AuthorizationLifecycle(verifier, policy, protocol)
    return bridge, private, challenge, policy, protocol, verifier


def _sign(private, fields):
    return private.sign(canonical_payload(fields))


def test_valid_synthetic_evidence(tmp_path):
    bridge, private, challenge, policy, _, _ = _setup(tmp_path)
    result = bridge.evaluate(challenge, _sign(private, challenge))

    assert result.accepted is True
    assert result.identity_verified is True
    assert result.execution_enabled is False
    assert policy.stored_state("auth-1") == "PENDING"


def test_invalid_signature(tmp_path):
    bridge, _, challenge, _, _, _ = _setup(tmp_path)
    other, _ = _keypair()

    result = bridge.evaluate(challenge, _sign(other, challenge))
    assert result.accepted is False
    assert result.identity_verified is False


def test_incorrect_repository(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    challenge["repository"] = "other/repo"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_incorrect_action(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    challenge["action"] = "deploy"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_incorrect_digest(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    challenge["action_digest"] = "other"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_incorrect_environment(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    challenge["environment"] = "production"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_incorrect_task(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    challenge["task_id"] = "other"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_incorrect_sha(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    challenge["expected_sha"] = "b" * 40

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_expired_authorization(tmp_path):
    bridge, private, challenge, policy, _, _ = _setup(tmp_path)
    created = _now() - timedelta(hours=2)
    policy.submit(
        AuthorizationRequest(
            authorization_id="auth-expired",
            chief_identity="chief-ref",
            requesting_participant="chief",
            action="merge_pull_request",
            action_digest="digest-a",
            repository="SGGlobal/QLA",
            project_id="qla",
            task_id="task-1",
            expected_sha="a" * 40,
            environment="local",
            created_at=created,
            expires_at=created + timedelta(minutes=30),
        )
    )
    challenge["authorization_id"] = "auth-expired"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_revoked_authorization(tmp_path):
    bridge, private, challenge, policy, _, _ = _setup(tmp_path)
    policy.revoke("auth-1")

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False
    assert result.policy_state == "REVOKED"


def test_consumed_authorization(tmp_path):
    bridge, private, challenge, policy, _, _ = _setup(tmp_path)
    policy.submit(
        AuthorizationRequest(
            authorization_id="auth-consumed",
            chief_identity="chief-ref",
            requesting_participant="chief",
            action="merge_pull_request",
            action_digest="digest-a",
            repository="SGGlobal/QLA",
            project_id="qla",
            task_id="task-1",
            expected_sha="a" * 40,
            environment="local",
            created_at=_now() - timedelta(minutes=1),
            expires_at=_now() + timedelta(hours=1),
            state=AuthorizationState.CONSUMED,
        )
    )
    challenge["authorization_id"] = "auth-consumed"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_replayed_challenge(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    signature = _sign(private, challenge)

    first = bridge.evaluate(challenge, signature)
    second = bridge.evaluate(challenge, signature)

    assert first.accepted is True
    assert second.accepted is False
    assert second.execution_enabled is False


def test_unknown_authorization(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    challenge["authorization_id"] = "missing"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_policy_halt(tmp_path):
    bridge, private, challenge, policy, _, _ = _setup(tmp_path)
    policy.halt()

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.reason == "Authorization policy is halted"
    assert result.execution_enabled is False


def test_protocol_halt(tmp_path):
    bridge, private, challenge, _, protocol, _ = _setup(tmp_path)
    protocol.halt()

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.reason == "Protocol is halted"
    assert result.execution_enabled is False


def test_impersonation(tmp_path):
    bridge, private, challenge, policy, _, _ = _setup(tmp_path)
    policy.submit(
        AuthorizationRequest(
            authorization_id="auth-ai",
            chief_identity="jason",
            requesting_participant="jason",
            action="merge_pull_request",
            action_digest="digest-a",
            repository="SGGlobal/QLA",
            project_id="qla",
            task_id="task-1",
            expected_sha="a" * 40,
            environment="local",
            created_at=_now() - timedelta(minutes=1),
            expires_at=_now() + timedelta(hours=1),
            state=AuthorizationState.PENDING,
        )
    )
    challenge["authorization_id"] = "auth-ai"

    result = bridge.evaluate(challenge, _sign(private, challenge))
    assert result.accepted is False


def test_verification_succeeds_policy_fails(tmp_path):
    bridge, private, challenge, policy, _, verifier = _setup(tmp_path)
    policy.revoke("auth-1")

    result = bridge.evaluate(challenge, _sign(private, challenge))

    assert result.identity_verified is True
    assert result.accepted is False
    assert result.execution_enabled is False
    assert result.policy_state == "REVOKED"


def test_concurrent_attempts(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    signature = _sign(private, challenge)

    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait(timeout=2)
        results.append(bridge.evaluate(challenge, signature))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    accepted = [result for result in results if result.accepted]
    assert len(accepted) <= 1
    assert all(result.execution_enabled is False for result in results)


def test_persistent_audit(tmp_path):
    bridge, private, challenge, policy, _, _ = _setup(tmp_path)
    bridge.evaluate(challenge, _sign(private, challenge))

    assert "Submitted" in policy.events_for("auth-1")


def test_execution_disabled(tmp_path):
    bridge, private, challenge, _, _, _ = _setup(tmp_path)
    result = bridge.evaluate(challenge, _sign(private, challenge))

    assert isinstance(result, LifecycleResult)
    assert result.execution_enabled is False
