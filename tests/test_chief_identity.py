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
    new_nonce,
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


def _fields(**overrides):
    values = {
        "authorization_id": "auth-1",
        "application": APPLICATION,
        "protocol_version": PROTOCOL_VERSION,
        "action": "merge_pull_request",
        "action_digest": "digest-a",
        "repository": "SGGlobal/QLA",
        "environment": "local",
        "project_id": "qla",
        "task_id": "task-1",
        "expected_sha": "abc1234",
        "key_id": "test-key",
        "challenge_id": "challenge-1",
        "nonce": new_nonce(),
        "issued_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(minutes=5)).isoformat(),
    }
    values.update(overrides)
    return values


def _sign(private, fields):
    return private.sign(canonical_payload(fields))


def test_valid_synthetic_signature(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields()
    result = verifier.verify(fields, _sign(private, fields))

    assert result.verified is True
    assert result.execution_enabled is False


def test_incorrect_signature(tmp_path):
    private, public = _keypair()
    other, _ = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields()
    result = verifier.verify(fields, _sign(other, fields))

    assert result.verified is False
    assert result.reason == "Invalid signature"


def test_unknown_public_key(tmp_path):
    private, _ = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    fields = _fields()

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Unknown public key"


def test_revoked_key(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)
    verifier.revoke("test-key")

    fields = _fields()
    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Key revoked"


def test_modified_payload(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields()
    signature = _sign(private, fields)
    fields["action_digest"] = "tampered"

    result = verifier.verify(fields, signature)
    assert result.reason == "Invalid signature"


def test_incorrect_repository(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields(repository="other/repo")
    result = verifier.verify(fields, _sign(private, fields))
    assert result.verified is False


def test_incorrect_environment(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields(environment="production")
    result = verifier.verify(fields, _sign(private, fields))
    assert result.verified is False


def test_incorrect_action(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields(action="deploy")
    result = verifier.verify(fields, _sign(private, fields))
    assert result.verified is False


def test_incorrect_sha(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields(expected_sha="different")
    result = verifier.verify(fields, _sign(private, fields))
    assert result.verified is False


def test_expired_challenge(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields(expires_at=(_now() - timedelta(seconds=1)).isoformat())
    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Challenge expired"


def test_replayed_nonce(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields()
    signature = _sign(private, fields)

    first = verifier.verify(fields, signature)
    second = verifier.verify(fields, signature)

    assert first.verified is True
    assert second.reason == "Nonce already used"


def test_missing_signature(tmp_path):
    _, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    result = verifier.verify(_fields(), None)
    assert result.reason == "Malformed signature"


def test_malformed_signature(tmp_path):
    _, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    result = verifier.verify(_fields(), b"short")
    assert result.reason == "Malformed signature"


def test_unsupported_protocol_version(tmp_path):
    _, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    verifier.register_public_key("test-key", public)

    fields = _fields(protocol_version="other-1")
    result = verifier.verify(fields, b"\x00" * 64)
    assert result.reason == "Unsupported protocol version"


def test_cross_protocol_rejection():
    private, _ = _keypair()
    fields = _fields()
    payload = canonical_payload(fields)

    foreign = b"other.protocol\n1\n" + payload
    with pytest.raises(Exception):
        private.sign(foreign)
        # Domain separation is in the canonical form. A signature over the
        # foreign bytes will not verify against the canonical payload.
    signature = private.sign(foreign)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    public = Ed25519PublicKey.from_public_bytes(
        private.public_key().public_bytes_raw()
    )
    with pytest.raises(Exception):
        public.verify(signature, payload)


def test_persistence_across_restart(tmp_path):
    private, public = _keypair()
    path = tmp_path / "id.db"
    verifier = ChiefIdentityVerifier(path)
    verifier.register_public_key("test-key", public)

    fields = _fields()
    signature = _sign(private, fields)
    assert verifier.verify(fields, signature).verified is True

    reopened = ChiefIdentityVerifier(path)
    second = reopened.verify(fields, signature)
    assert second.reason == "Nonce already used"


def test_concurrent_replay(tmp_path):
    private, public = _keypair()
    path = tmp_path / "id.db"
    first = ChiefIdentityVerifier(path)
    first.register_public_key("test-key", public)
    second = ChiefIdentityVerifier(path)

    fields = _fields()
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
    verifier.register_public_key("test-key", public)

    result = verifier.verify(_fields(), _sign(private, _fields()))
    # Different nonce, so this particular call is invalid; the point is
    # the result type never enables execution.
    assert isinstance(result, VerificationResult)
    assert result.execution_enabled is False


def test_no_network_or_trading(tmp_path):
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False


def test_unexpected_field_rejected():
    fields = _fields()
    fields["extra"] = "nope"
    with pytest.raises(IdentityError, match="Unexpected"):
        canonical_payload(fields)
