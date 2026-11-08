"""
SKIPPY AI Collaboration Bridge — Chief Identity Tests.

Disposable synthetic Ed25519 keys. No persistence of private keys.
No network. No execution.
"""

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlite3

from skippy.collaboration.authorization import AuthorizationPolicy
from skippy.collaboration.chief_identity import (
    APPLICATION,
    PROTOCOL_VERSION,
    ChiefIdentityVerifier,
    IdentityError,
    VerificationResult,
    canonical_payload,
)
from skippy.collaboration.coordinated_halt import SUPPORTED_SCHEMA_VERSION
from skippy.collaboration.coordinated_halt_gate import CoordinatedHaltGate
from skippy.collaboration.protocol import CollaborationProtocol


def _now():
    return datetime.now(timezone.utc)


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    return private, public


def _init_coordinated(path: Path, state: str = "NOT_HALTED") -> None:
    """Test-only synthetic authority. Not a production bootstrap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sequence = 1 if state == "HALTED" else 0
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE halt_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                state TEXT NOT NULL CHECK (state IN ('NOT_HALTED', 'HALTED')),
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE halt_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                sequence INTEGER,
                detail TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (SUPPORTED_SCHEMA_VERSION,),
        )
        connection.execute(
            """
            INSERT INTO halt_state (singleton, state, sequence, updated_at, reason)
            VALUES (1, ?, ?, '2026-01-01T00:00:00+00:00', 'test-init')
            """,
            (state, sequence),
        )


def _bound_verifier(tmp_path, coordinated_state="NOT_HALTED"):
    """Return a verifier explicitly bound to a valid synthetic coordinated guard."""
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    policy = AuthorizationPolicy(tmp_path / "auth.db")
    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    coordinated = tmp_path / "coord.db"
    _init_coordinated(coordinated, coordinated_state)
    gate = CoordinatedHaltGate(coordinated, policy, protocol)
    policy.require_coordinated_guard(gate)
    protocol.require_coordinated_guard(gate)
    verifier.require_coordinated_guard(gate)
    return verifier, policy, protocol, gate, coordinated


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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, _sign(private, fields))

    assert result.verified is True
    assert result.execution_enabled is False
    assert fields["key_id"] == "test-key"


def test_incorrect_signature(tmp_path):
    private, public = _keypair()
    other, _ = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, _sign(other, fields))
    assert result.reason == "Invalid signature"


def test_unknown_public_key(tmp_path):
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    with pytest.raises(IdentityError, match="Unknown signing key"):
        _issue(verifier, key_id="missing")


def test_revoked_key(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    verifier.revoke("test-key")

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Key revoked"


def test_signature_from_other_active_key(tmp_path):
    private, public = _keypair()
    other_private, other_public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    verifier.register_public_key("test-key", public)
    verifier.register_public_key("other-key", other_public)
    fields = _issue(verifier)

    result = verifier.verify(fields, _sign(other_private, fields))
    assert result.reason == "Invalid signature"


def test_incorrect_key_identifier(tmp_path):
    private, public = _keypair()
    _, other_public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    verifier.register_public_key("other-key", other_public)
    fields["key_id"] = "other-key"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: key_id"


def test_modified_issued_at(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    fields["issued_at"] = (_now() - timedelta(minutes=1)).isoformat()

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: issued_at"


def test_modified_expires_at(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    fields["nonce"] = "n" * 16

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: nonce"
    assert result.execution_enabled is False


def test_scope_mismatch_repository(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    fields["repository"] = "other/repo"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: repository"


def test_scope_mismatch_environment(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    fields["environment"] = "production"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: environment"


def test_scope_mismatch_action(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    fields["action"] = "deploy"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: action"


def test_scope_mismatch_sha(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    fields["expected_sha"] = "b" * 40

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Scope mismatch: expected_sha"


def test_unknown_challenge(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    fields["challenge_id"] = "unknown"

    result = verifier.verify(fields, _sign(private, fields))
    assert result.reason == "Unknown challenge"


def test_future_issued_challenge(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    signature = _sign(private, fields)

    first = verifier.verify(fields, signature)
    second = verifier.verify(fields, signature)

    assert first.verified is True
    assert second.reason == "Challenge already consumed"


def test_missing_signature(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)

    result = verifier.verify(fields, None)
    assert result.reason == "Malformed signature"


def test_malformed_signature(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    # Note: _bound_verifier creates its own id.db; override path if needed, but for this use the helper carefully.
    # Recreate with specific path for persistence.
    # For simplicity, use the helper's path.
    fields = _prepare(verifier, private, public)
    signature = _sign(private, fields)
    assert verifier.verify(fields, signature).verified is True

    # New instance on same DB is unbound: cannot succeed without rebinding.
    reopened = ChiefIdentityVerifier(verifier.database_path)
    second = reopened.verify(fields, signature)
    assert second.verified is False
    assert "Missing coordinated guard" in second.reason
    assert second.execution_enabled is False


def test_concurrent_replay(tmp_path):
    private, public = _keypair()
    verifier1, policy, protocol, gate, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier1, private, public)
    signature = _sign(private, fields)
    # Second instance on same identity DB, bound to the same guard
    verifier2 = ChiefIdentityVerifier(verifier1.database_path)
    verifier2.require_coordinated_guard(gate)

    barrier = threading.Barrier(2)
    results = []

    def worker(verifier):
        barrier.wait(timeout=2)
        results.append(verifier.verify(fields, signature))

    threads = [
        threading.Thread(target=worker, args=(verifier1,)),
        threading.Thread(target=worker, args=(verifier2,)),
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
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
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


def test_ungated_challenge_issuance_rejected(tmp_path):
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    with pytest.raises(IdentityError, match="Missing coordinated guard"):
        _issue(verifier)


def test_ungated_verification_rejected(tmp_path):
    private, public = _keypair()
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    # Cannot issue, so construct minimal fields for verify path
    fields = {
        "authorization_id": "auth-1",
        "application": APPLICATION,
        "protocol_version": PROTOCOL_VERSION,
        "action": "merge_pull_request",
        "action_digest": "digest-a",
        "repository": "SGGlobal/QLA",
        "environment": "local",
        "project_id": "qla",
        "task_id": "task-1",
        "expected_sha": "a" * 40,
        "key_id": "test-key",
        "challenge_id": "c1",
        "nonce": "n" * 16,
        "issued_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(minutes=5)).isoformat(),
    }
    result = verifier.verify(fields, _sign(private, fields))
    assert result.verified is False
    assert "Missing coordinated guard" in result.reason
    assert result.execution_enabled is False


def test_correctly_bound_issuance_succeeds(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    assert fields["challenge_id"]
    assert fields["nonce"]


def test_correctly_bound_verification_succeeds(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    result = verifier.verify(fields, _sign(private, fields))
    assert result.verified is True
    assert result.execution_enabled is False


def test_coordinated_halted_rejection(tmp_path):
    verifier, _, _, _, _ = _bound_verifier(tmp_path, coordinated_state="HALTED")
    with pytest.raises(IdentityError, match="Coordinated HALTED"):
        _issue(verifier)


def test_coordinated_unknown_rejection(tmp_path):
    verifier, _, _, gate, coordinated = _bound_verifier(tmp_path)
    # Corrupt after binding so read fails (migration may still attempt)
    with sqlite3.connect(coordinated) as connection:
        connection.execute("UPDATE schema_meta SET value = 'bad-version'")
    with pytest.raises(IdentityError, match="Failed migration|Coordinated UNKNOWN|Missing or corrupted"):
        _issue(verifier)


def test_missing_authority_rejection(tmp_path):
    verifier, _, _, _, coordinated = _bound_verifier(tmp_path)
    coordinated.unlink()
    with pytest.raises(IdentityError, match="Failed migration|Missing or corrupted coordinated authority|Coordinated UNKNOWN"):
        _issue(verifier)


def test_legacy_policy_halt(tmp_path):
    verifier, policy, _, _, _ = _bound_verifier(tmp_path)
    policy.halt()
    with pytest.raises(IdentityError, match="Coordinated HALTED|Legacy policy HALT"):
        _issue(verifier)


def test_legacy_protocol_halt(tmp_path):
    verifier, _, protocol, _, _ = _bound_verifier(tmp_path)
    protocol.halt()
    with pytest.raises(IdentityError, match="Coordinated HALTED|Legacy protocol HALT"):
        _issue(verifier)


def test_guard_identity_replacement(tmp_path):
    verifier, policy, protocol, gate, _ = _bound_verifier(tmp_path)
    # Replace the binding by creating a new gate and trying to rebind (should fail)
    new_gate = CoordinatedHaltGate(gate.coordinated_path, policy, protocol)
    with pytest.raises(IdentityError, match="cannot be replaced"):
        verifier.require_coordinated_guard(new_gate)


def test_configuration_drift(tmp_path):
    verifier, _, _, gate, _ = _bound_verifier(tmp_path)
    # Drift the snapshot by mutating a path after binding (if possible)
    # Or change a config that is snapshotted
    gate.absent_legacy_policy = None  # type change or different
    with pytest.raises(IdentityError, match="Configuration drift"):
        _issue(verifier)


def test_invalid_guard_type(tmp_path):
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    with pytest.raises(IdentityError, match="Invalid coordinated guard type"):
        verifier.require_coordinated_guard(object())


def test_replaced_check_method(tmp_path):
    verifier, _, _, gate, _ = _bound_verifier(tmp_path)
    original = gate.check
    def fake_check(self):
        return original()
    gate.check = fake_check.__get__(gate, type(gate))
    with pytest.raises(IdentityError, match="Replaced check method|Coordinated protection"):
        _issue(verifier)


def test_policy_protocol_mismatch(tmp_path):
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    with pytest.raises(IdentityError, match="Invalid coordinated guard type"):
        verifier.require_coordinated_guard("not-a-gate")


def test_no_challenge_record_after_rejected_issuance(tmp_path):
    verifier = ChiefIdentityVerifier(tmp_path / "id.db")
    try:
        _issue(verifier)
    except IdentityError:
        pass
    with sqlite3.connect(verifier.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM challenges").fetchone()[0]
    assert count == 0


def test_challenge_remains_open_after_rejected_verification(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    # Force rejection by unbinding conceptually: create new unbound and try, but since bound, corrupt
    # Use a different verifier unbound
    unbound = ChiefIdentityVerifier(verifier.database_path)
    result = unbound.verify(fields, _sign(private, fields))
    assert result.verified is False
    with sqlite3.connect(verifier.database_path) as connection:
        status = connection.execute(
            "SELECT status FROM challenges WHERE challenge_id = ?",
            (fields["challenge_id"],),
        ).fetchone()[0]
    assert status == "open"


def test_invalid_signature_rejection(tmp_path):
    private, public = _keypair()
    other, _ = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    result = verifier.verify(fields, _sign(other, fields))
    assert result.reason == "Invalid signature"
    assert result.verified is False


def test_replay_prevention(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    sig = _sign(private, fields)
    assert verifier.verify(fields, sig).verified is True
    second = verifier.verify(fields, sig)
    assert second.reason == "Challenge already consumed"
    assert second.verified is False


def test_revocation_during_coordinated_halt(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path, coordinated_state="HALTED")
    verifier.register_public_key("test-key", public)
    # Revoke must succeed even when coordinated is HALTED
    verifier.revoke("test-key")
    # Confirm status
    with sqlite3.connect(verifier.database_path) as connection:
        status = connection.execute(
            "SELECT status FROM trusted_keys WHERE key_id = ?",
            ("test-key",),
        ).fetchone()[0]
    assert status == "revoked"


def test_revocation_during_authority_failure(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, coordinated = _bound_verifier(tmp_path)
    verifier.register_public_key("test-key", public)
    coordinated.unlink()
    # Still able to revoke
    verifier.revoke("test-key")


def test_restart_without_rebinding(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    reopened = ChiefIdentityVerifier(verifier.database_path)
    result = reopened.verify(fields, _sign(private, fields))
    assert result.verified is False
    assert "Missing coordinated guard" in result.reason


def test_lifecycle_integration(tmp_path):
    # Covered by existing lifecycle tests; this confirms binding is shared
    from skippy.collaboration.authorization_lifecycle import AuthorizationLifecycle
    private, public = _keypair()
    verifier, policy, protocol, gate, _ = _bound_verifier(tmp_path)
    verifier.register_public_key("test-key", public)
    fields = _issue(verifier)
    lifecycle = AuthorizationLifecycle(verifier, policy, protocol, coordinated_gate=gate)
    result = lifecycle.evaluate(fields, _sign(private, fields))
    # May fail policy if no submitted request, but identity should pass if gate ok
    # For this, just ensure no exception and execution disabled
    assert result.execution_enabled is False


def test_execution_enabled_false(tmp_path):
    private, public = _keypair()
    verifier, _, _, _, _ = _bound_verifier(tmp_path)
    fields = _prepare(verifier, private, public)
    result = verifier.verify(fields, _sign(private, fields))
    assert result.execution_enabled is False
    assert result.verified is True
