"""
SKIPPY AI Collaboration Bridge — Chief Identity Tests.

Disposable synthetic Ed25519 keys. No persistence of private keys.
No network. No execution.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from skippy.collaboration.chief_identity import (
    APPLICATION,
    DOMAIN,
    PROTOCOL_VERSION,
    ChiefIdentityVerifier,
    IdentityError,
    VerificationResult,
    canonical_payload,
)


def _now():
    return datetime.now(timezone.utc)


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    return private, public


def _issue(verifier, **overrides):
    values = {
        "authorization_id": "auth-1",
        "action": "merge_pull_request",
        "action_digest": "digest-a",
        "repository": "SGGlobal/QLA",
        "environment": "local",
        "project_id": "qla",
        "task_id": "task-1",
        "expected_sha": "a" * 40,
    }
    values.update(overrides)
    return verifier.issue_challenge(**values)


def _sign(private, fields):
    return private.sign(canonical_payload(fields))


def _prepare(verifier, private, public, **overrides):
    verifier.register_public_key("test-key", public)
    challenge = _issue(verifier, **overrides)
    challenge["key_id"] = "test-key"
    return challenge


def test_valid_synthetic_signature(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, _sign(private, fields))

    assert result.verified is True
    assert result.execution_enabled is False


def test_incorrect_signature(tmp_path):
    private, public = _keypair()
    other, _ = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, _sign(other, fields))
    assert result.reason == "Invalid signature"


def test_unknown_public_key(tmp_path):
    private, _ = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    challenge = _issue(verifier)
    challenge["key_id"] = "missing"

    result = verifier.verify(challenge, _sign(private, challenge))
    assert result.reason == "Unknown public key"


def test_revoked_key(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    verifier.revoke("test-key")

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Key revoked"


def test_scope_mismatch_repository(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["repository"] = "other/repo"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: repository"


def test_scope_mismatch_environment(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["environment"] = "production"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: environment"


def test_scope_mismatch_action(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["action"] = "deploy"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: action"


def test_scope_mismatch_sha(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["expected_sha"] = "b" * 40

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: expected_sha"


def test_scope_mismatch_authorization_id(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["authorization_id"] = "other"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: authorization_id"


def test_unknown_challenge(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["challenge_id"] = "unknown"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Unknown challenge"


def test_expired_challenge(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)
    past = _now() - timedelta(minutes=10)
    challenge = verifier.issue_challenge(
        authorization_id="auth-1",
        action="merge_pull_request",
        action_digest="digest-a",
        repository="SGGlobal/QLA",
        environment="local",
        project_id="qla",
        task_id="task-1",
        expected_sha="a" * 40,
        now=past,
        lifetime=timedelta(minutes=1),
    )
    challenge["key_id"] = "test-key"

    result = verifier.verify(challenge, _sign(private, challenge))
    assert result.reason == "Challenge expired"


def test_future_issued_challenge(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)
    future = _now() + timedelta(minutes=5)
    challenge = verifier.issue_challenge(
        authorization_id="auth-1",
        action="merge_pull_request",
        action_digest="digest-a",
        repository="SGGlobal/QLA",
        environment="local",
        project_id="qla",
        task_id="task-1",
        expected_sha="a" * 40,
        now=future,
    )
    challenge["key_id"] = "test-key"

    result = verifier.verify(challenge, _sign(private, challenge))
    assert result.reason == "Challenge issued in the future"


def test_replayed_nonce(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    signature = _sign(private, fields)

    first = verifier.verify(fields, signature)
    second = verifier.verify(fields, signature)

    assert first.verified is True
    assert second.reason == "Challenge already consumed"


def test_missing_signature(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, None)
    assert result.reason == "Malformed signature"


def test_malformed_signature(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, b"short")
    assert result.reason == "Malformed signature"


def test_unsupported_protocol_version(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["protocol_version"] = "other-1"

    result = verifier.verify(fields, b"\x00" * 64)
    assert result.reason == "Unsupported protocol version"


def test_cross_protocol_rejection(tmp_path):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)

    skippy = canonical_payload(fields)
    foreign = f"other.protocol\n1\n{skippy.decode('utf-8')}".encode("utf-8")
    signature = private.sign(foreign)

    key = Ed25519PublicKey.from_public_bytes(public)
    with pytest.raises(InvalidSignature):
        key.verify(signature, skippy)

    result = verifier.verify(fields, signature)
    assert result.reason == "Invalid signature"


def test_persistence_across_restart(tmp_path):
    private, public = _keypair()
    path = tmp_path / "id.db"
    verifier = ChiefIdentityVerifier(path)
    fields = _prepare(verifier, private, public)
    signature = _sign(private, fields)
    assert verifier.verify(fields, signature).verified is True

    reopened = ChiefIdentityVerifier(path)
    second = reopened.verify(fields, signature)
    assert second.reason == "Challenge already consumed"


def test_concurrent_replay(tmp_path):
    private, public = _keypair()
    path = tmp_path / "id.db"
    first = ChiefIdentityVerifier(path)
    fields = _prepare(first, private, public)
    second = ChiefIdentityVerifier(path)
    signature = _sign(private, fields)

    barrier = threading.Barrier(2)
    results = []

    def worker(verifier):
        barrier.wait(timeout=2)
        results.append(verifier.verify(fields, signature))

    threads = [
        threading.Thread(target=worker, args=(first,)),
        threading.Thread(target=worker, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    verified = [result for result in results if result.verified]
    rejected = [result for result in results if not result.verified]
    assert len(verified) == 1
    assert len(rejected) == 1
    assert all(result.execution_enabled is False for result in results)


def test_no_execution_capability(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, _sign(private, fields))
    assert isinstance(result, VerificationResult)
    assert result.verified is True
    assert result.execution_enabled is False


def test_malformed_timestamp(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["issued_at"] = "not-a-time"

    result = verifier.verify(fields, b"\x00" * 64)
    assert result.reason == "Malformed issued_at"


def test_wrong_field_type(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["action"] = 1

    result = verifier.verify(fields, b"\x00" * 64)
    assert "must be a nonempty string" in result.reason


def test_unexpected_field_rejected():
    fields = {
        "authorization_id": "a",
        "application": APPLICATION,
        "protocol_version": PROTOCOL_VERSION,
        "action": "merge_pull_request",
        "action_digest": "d",
        "repository": "r",
        "environment": "local",
        "project_id": "p",
        "task_id": "t",
        "expected_sha": "a" * 40,
        "key_id": "k",
        "challenge_id": "c",
        "nonce": "n" * 16,
        "issued_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(minutes=1)).isoformat(),
        "extra": "nope",
    }
    with pytest.raises(IdentityError, match="Unexpected"):
        canonical_payload(fields)


def test_no_network_or_trading(tmp_path):
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False
