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
        "key_id": "test-key",
    }
    values.update(overrides)
    return verifier.issue_challenge(**values)


def _sign(private, fields):
    return private.sign(canonical_payload(fields))


def _prepare(verifier, private, public, **overrides):
    verifier.register_public_key("test-key", public)
    return _issue(verifier, **overrides)


def test_valid_synthetic_signature(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, _sign(private, fields))

    assert result.verified is True
    assert result.execution_enabled is False
    assert fields["key_id"] == "test-key"


def test_incorrect_signature(tmp_path):
    private, public = _keypair()
    other, _ = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, _sign(other, fields))
    assert result.reason == "Invalid signature"


def test_unknown_public_key(tmp_path):
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    with pytest.raises(IdentityError, match="Unknown signing key"):
        _issue(verifier, key_id="missing")


def test_revoked_key(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    verifier.revoke("test-key")

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Key revoked"


def test_signature_from_other_active_key(tmp_path):
    private, public = _keypair()
    other_private, other_public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)
    verifier.register_public_key("other-key", other_public)
    fields = _issue(verifier)

    result = verifier.verify(fields, _sign(other_private, fields))
    assert result.reason == "Invalid signature"


def test_incorrect_key_identifier(tmp_path):
    private, public = _keypair()
    _, other_public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    verifier.register_public_key("other-key", other_public)
    fields["key_id"] = "other-key"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: key_id"


def test_modified_issued_at(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["issued_at"] = (_now() - timedelta(minutes=1)).isoformat()

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: issued_at"


def test_modified_expires_at(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["expires_at"] = (_now() + timedelta(minutes=10)).isoformat()

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: expires_at"


def test_expired_registry_despite_altered_payload(tmp_path):
    """
    Fixed reference time.

    Issued 4 minutes before reference. Original expiration 3 minutes
    before reference. Modified signed expiration 2 minutes after reference.
    The modified payload stays within the 15-minute maximum lifetime.
    """

    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    reference = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    issued = reference - timedelta(minutes=4)
    original_expires = reference - timedelta(minutes=3)

    fields = verifier.issue_challenge(
        authorization_id="auth-1",
        action="merge_pull_request",
        action_digest="digest-a",
        repository="SGGlobal/QLA",
        environment="local",
        project_id="qla",
        task_id="task-1",
        expected_sha="a" * 40,
        key_id="test-key",
        now=issued,
        lifetime=original_expires - issued,
    )

    fields["expires_at"] = (reference + timedelta(minutes=2)).isoformat()
    canonical_payload(fields)
    signature = _sign(private, fields)

    result = verifier.verify(fields, signature, now=reference)

    assert result.reason == "Challenge expired"
    assert result.execution_enabled is False

    # Rejected attempt does not consume the challenge.
    second = verifier.verify(fields, signature, now=reference)
    assert second.reason == "Challenge expired"


def test_unchanged_expired_challenge(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    reference = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
    issued = reference - timedelta(minutes=4)

    fields = verifier.issue_challenge(
        authorization_id="auth-1",
        action="merge_pull_request",
        action_digest="digest-a",
        repository="SGGlobal/QLA",
        environment="local",
        project_id="qla",
        task_id="task-1",
        expected_sha="a" * 40,
        key_id="test-key",
        now=issued,
        lifetime=timedelta(minutes=1),
    )

    result = verifier.verify(fields, _sign(private, fields), now=reference)
    assert result.reason == "Challenge expired"
    assert result.execution_enabled is False


def test_modified_nonce_rejected(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["nonce"] = "n" * 16

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: nonce"
    assert result.execution_enabled is False


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


def test_unknown_challenge(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _prepare(verifier, private, public)
    fields["challenge_id"] = "unknown"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Unknown challenge"


def test_future_issued_challenge(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)
    future = _now() + timedelta(minutes=5)
    fields = verifier.issue_challenge(
        authorization_id="auth-1",
        action="merge_pull_request",
        action_digest="digest-a",
        repository="SGGlobal/QLA",
        environment="local",
        project_id="qla",
        task_id="task-1",
        expected_sha="a" * 40,
        key_id="test-key",
        now=future,
    )

    result = verifier.verify(fields, _sign(private, fields))
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


def test_malformed_sha():
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
        "expected_sha": "Z" * 40,
        "key_id": "k",
        "challenge_id": "c",
        "nonce": "n" * 16,
        "issued_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(minutes=1)).isoformat(),
    }
    with pytest.raises(IdentityError, match="expected_sha"):
        canonical_payload(fields)


def test_canonical_serialization():
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
    }
    payload = canonical_payload(fields)
    assert payload.startswith(b"skippy.chief.authorization\nchief-auth-1\n")


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


def test_duplicate_key_rejected(tmp_path):
    _, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    with pytest.raises(IdentityError, match="Duplicate"):
        verifier.register_public_key("test-key", public)


def test_no_network_or_trading(tmp_path):
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False
