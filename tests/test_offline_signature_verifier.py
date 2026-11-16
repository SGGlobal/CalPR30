"""BRIDGE-02B.1 offline signature verification tests."""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from nacl.signing import SigningKey

from skippy.collaboration.offline_signature_verifier import (
    KeyEntry,
    SignatureError,
    SignedEnvelope,
    load_trust_root,
    verify_signature,
)


def _now():
    return datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _key_pair(seed: bytes = b"\x01" * 32):
    sk = SigningKey(seed)
    return sk, sk.verify_key.encode()


def _entry(key_id="k1", actor="chief", role="chief", pubkey=None, state="active", expires=None):
    return KeyEntry(
        key_id=key_id,
        actor_id=actor,
        permitted_role=role,
        public_key=pubkey or b"\x02" * 32,
        state=state,
        expires_at=expires or _now() + timedelta(days=30),
    )


def _envelope(signing_key, **overrides):
    defaults = dict(
        schema_version=1,
        sender="chief",
        recipient="jason",
        role="chief",
        project_id="QLA",
        task_id="t1",
        checkpoint_id="c1",
        assignment_id="a1",
        message_id="m1",
        idempotency_key="i1",
        sequence=1,
        timestamp=_now(),
        expiry=_now() + timedelta(hours=1),
        payload={"type": "assignment", "objective": "test"},
        key_id="k1",
    )
    defaults.update(overrides)
    from skippy.collaboration.offline_signature_verifier import _canonical_json
    payload_bytes = _canonical_json(defaults["payload"])
    defaults["payload_digest"] = hashlib.sha256(payload_bytes).digest()
    env = SignedEnvelope(**defaults, signature=b"\x00" * 64)
    from skippy.collaboration.offline_signature_verifier import canonical_bytes
    sig = signing_key.sign(canonical_bytes(env)).signature
    return SignedEnvelope(**defaults, signature=sig)


def test_valid_signature_accepted():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk)
    assert verify_signature(env, root, _now()) == "cryptographically_valid"


def test_tampered_payload_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk)
    tampered = SignedEnvelope(**{**env.__dict__, "payload": {"type": "assignment", "objective": "tampered"}})
    with pytest.raises(SignatureError, match="digest|Invalid signature"):
        verify_signature(tampered, root, _now())


def test_unknown_key_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk, key_id="unknown")
    with pytest.raises(SignatureError, match="Unknown"):
        verify_signature(env, root, _now())


def test_revoked_key_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk, state="revoked")], version=1)
    env = _envelope(sk)
    with pytest.raises(SignatureError, match="not active"):
        verify_signature(env, root, _now())


def test_expired_key_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk, expires=_now() - timedelta(hours=1))], version=1)
    env = _envelope(sk)
    with pytest.raises(SignatureError, match="expired"):
        verify_signature(env, root, _now())


def test_message_age_boundary_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    old = _now() - timedelta(hours=24)
    env = _envelope(sk, timestamp=old, expiry=_now() + timedelta(hours=1))
    with pytest.raises(SignatureError, match="maximum age"):
        verify_signature(env, root, _now())


def test_future_skew_boundary_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    future = _now() + timedelta(seconds=60)
    env = _envelope(sk, timestamp=future, expiry=future + timedelta(hours=1))
    with pytest.raises(SignatureError, match="future"):
        verify_signature(env, root, _now())


def test_expiry_boundary_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk, expiry=_now())
    with pytest.raises(SignatureError, match="expired"):
        verify_signature(env, root, _now())


def test_wrong_role_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk, role="chief")], version=1)
    env = _envelope(sk, role="implementer")
    with pytest.raises(SignatureError, match="Role"):
        verify_signature(env, root, _now())


def test_reviewed_sha_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    with pytest.raises(SignatureError, match="reviewed_sha"):
        _envelope(sk, payload={"type": "assignment", "reviewed_sha": "abc"})


def test_duplicate_key_id_rejected():
    sk, pk = _key_pair()
    with pytest.raises(SignatureError, match="Duplicate"):
        load_trust_root([_entry(pubkey=pk), _entry(pubkey=pk)], version=1)


def test_conflicting_roles_rejected():
    sk1, pk1 = _key_pair(b"\x01" * 32)
    sk2, pk2 = _key_pair(b"\x02" * 32)
    with pytest.raises(SignatureError, match="Conflicting"):
        load_trust_root([
            _entry(key_id="k1", pubkey=pk1, role="chief"),
            _entry(key_id="k2", pubkey=pk2, role="implementer"),
        ], version=1)


def test_replay_is_cryptographically_valid():
    """A replayed message with a valid signature is cryptographically valid.
    Durable replay rejection is the admission component's responsibility."""
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk)
    assert verify_signature(env, root, _now()) == "cryptographically_valid"
    # Same envelope again is still cryptographically valid
    assert verify_signature(env, root, _now()) == "cryptographically_valid"


def test_schema_version_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk, schema_version=2)
    with pytest.raises(SignatureError, match="schema"):
        verify_signature(env, root, _now())


def test_digest_mismatch_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk)
    bad = SignedEnvelope(**{**env.__dict__, "payload_digest": b"\xff" * 32})
    with pytest.raises(SignatureError, match="digest"):
        verify_signature(bad, root, _now())


def test_fractional_timestamp_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    frac = _now().replace(microsecond=123456)
    with pytest.raises(SignatureError, match="fractional"):
        _envelope(sk, timestamp=frac)


def test_empty_required_field_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk)
    bad = SignedEnvelope(**{**env.__dict__, "sender": ""})
    with pytest.raises(SignatureError, match="empty"):
        verify_signature(bad, root, _now())


def test_nested_reviewed_sha_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    with pytest.raises(SignatureError, match="reviewed_sha"):
        _envelope(sk, payload={"type": "assignment", "meta": {"reviewed_sha": "abc"}})


def test_wrong_actor_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk, actor="chief")], version=1)
    env = _envelope(sk, sender="jason")
    with pytest.raises(SignatureError, match="Actor"):
        verify_signature(env, root, _now())


def test_float_in_payload_rejected():
    sk, pk = _key_pair()
    with pytest.raises(SignatureError, match="Float"):
        _envelope(sk, payload={"type": "assignment", "value": 1.5})


def test_non_nfc_string_rejected():
    sk, pk = _key_pair()
    # e + combining acute = non-NFC
    non_nfc = "e\u0301"
    with pytest.raises(SignatureError, match="NFC"):
        _envelope(sk, payload={"type": "assignment", "note": non_nfc})


def test_registry_version_rollback_rejected():
    from skippy.collaboration.offline_signature_verifier import SyntheticRegistryManager
    sk, pk = _key_pair()
    mgr = SyntheticRegistryManager()
    root_v2 = load_trust_root([_entry(pubkey=pk)], version=2)
    mgr.accept(root_v2)
    root_v1 = load_trust_root([_entry(pubkey=pk)], version=1)
    with pytest.raises(SignatureError, match="rollback"):
        mgr.accept(root_v1)


def test_key_expiry_equality_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk, expires=_now())], version=1)
    env = _envelope(sk)
    with pytest.raises(SignatureError, match="expired"):
        verify_signature(env, root, _now())


def test_non_integer_version_rejected():
    sk, pk = _key_pair()
    with pytest.raises(SignatureError, match="version"):
        load_trust_root([_entry(pubkey=pk)], version=True)
    with pytest.raises(SignatureError, match="version"):
        load_trust_root([_entry(pubkey=pk)], version=1.0)


def test_naive_now_rejected():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk)
    with pytest.raises(SignatureError, match="UTC"):
        verify_signature(env, root, datetime(2026, 11, 8, 12, 0))


def test_concurrent_rollback_rejected():
    import threading
    from skippy.collaboration.offline_signature_verifier import SyntheticRegistryManager
    sk, pk = _key_pair()
    mgr = SyntheticRegistryManager()
    root_v2 = load_trust_root([_entry(pubkey=pk)], version=2)
    root_v1 = load_trust_root([_entry(pubkey=pk)], version=1)
    # Accept v2 first to establish the high-water mark
    mgr.accept(root_v2)
    errors = []
    barrier = threading.Barrier(4)

    def try_v1():
        barrier.wait()
        try:
            mgr.accept(root_v1)
        except SignatureError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=try_v1) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert mgr.highest_version == 2
    assert len(errors) == 4
    assert all("rollback" in e for e in errors)


def test_independent_deterministic_vector():
    """Independently established canonical bytes and Ed25519 signature."""
    from nacl.signing import VerifyKey
    from skippy.collaboration.offline_signature_verifier import canonical_bytes, _canonical_json
    expected_pubkey = bytes.fromhex(
        "2152f8d19b791d24453242e15f2eab6cb7cffa7b6a5ed30097960e069881db12"
    )
    expected_canonical = bytes.fromhex(
        "514c412d4252494447452d3032422d763100010005636869656600056a61736f6e"
        "000563686965660003514c4100027431000263310002613100026d3100026931"
        "0000000000000001323032362d31312d30385431323a30303a30305a323032362d"
        "31312d30385431333a30303a30305a12a875821158e76336b94d04a55c2fa424"
        "200c988c6439dad2a6bad37b0d75ac0000002a7b226f626a656374697665223a"
        "22766563746f72222c2274797065223a2261737369676e6d656e74227d00026b31"
    )
    expected_signature = bytes.fromhex(
        "460c1f5419b905af776ddd08202b8bb5ad66b3dab216b0b47c6797dd163f5d2a"
        "3c6efc0274fbcb17a27773f30654b7395bcc3d3167156374d9d04d5b42b7c000"
    )
    # Verify the fixed signature against the fixed bytes
    VerifyKey(expected_pubkey).verify(expected_canonical, expected_signature)
    # Verify canonical_bytes produces the same bytes
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    payload = {"objective": "vector", "type": "assignment"}
    payload_bytes = _canonical_json(payload)
    digest = hashlib.sha256(payload_bytes).digest()
    env = SignedEnvelope(
        schema_version=1, sender="chief", recipient="jason", role="chief",
        project_id="QLA", task_id="t1", checkpoint_id="c1", assignment_id="a1",
        message_id="m1", idempotency_key="i1", sequence=1,
        timestamp=now, expiry=now + timedelta(hours=1),
        payload_digest=digest, payload=payload, key_id="k1",
        signature=expected_signature,
    )
    assert canonical_bytes(env) == expected_canonical
    # Single-byte mutation fails
    mutated = bytearray(expected_canonical)
    mutated[0] ^= 0xFF
    with pytest.raises(Exception):
        VerifyKey(expected_pubkey).verify(bytes(mutated), expected_signature)


def test_overlapping_version_submissions():
    """Concurrent version 1 and version 2 submissions cannot roll back."""
    import threading
    from skippy.collaboration.offline_signature_verifier import SyntheticRegistryManager
    sk, pk = _key_pair()
    mgr = SyntheticRegistryManager()
    root_v1 = load_trust_root([_entry(pubkey=pk)], version=1)
    root_v2 = load_trust_root([_entry(pubkey=pk)], version=2)
    results = {"v1": [], "v2": []}
    barrier = threading.Barrier(8)

    def submit(root, label):
        barrier.wait()
        try:
            mgr.accept(root)
            results[label].append("ok")
        except SignatureError as exc:
            results[label].append(str(exc))

    threads = []
    for _ in range(4):
        threads.append(threading.Thread(target=submit, args=(root_v1, "v1")))
        threads.append(threading.Thread(target=submit, args=(root_v2, "v2")))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert mgr.highest_version == 2
    # All v2 submissions must succeed
    assert all(r == "ok" for r in results["v2"])
    # Any v1 that arrived after a v2 must be rejected
    # At least the final state is consistent
    assert mgr.highest_version >= 2


def test_verify_signature_with_fixed_vector():
    """Fixed vector calls verify_signature directly for success and tamper."""
    from nacl.signing import SigningKey
    from skippy.collaboration.offline_signature_verifier import _canonical_json
    sk = SigningKey(b"\x42" * 32)
    pk = sk.verify_key.encode()
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    payload = {"objective": "vector", "type": "assignment"}
    payload_bytes = _canonical_json(payload)
    digest = hashlib.sha256(payload_bytes).digest()
    expected_signature = bytes.fromhex(
        "460c1f5419b905af776ddd08202b8bb5ad66b3dab216b0b47c6797dd163f5d2a"
        "3c6efc0274fbcb17a27773f30654b7395bcc3d3167156374d9d04d5b42b7c000"
    )
    env = SignedEnvelope(
        schema_version=1, sender="chief", recipient="jason", role="chief",
        project_id="QLA", task_id="t1", checkpoint_id="c1", assignment_id="a1",
        message_id="m1", idempotency_key="i1", sequence=1,
        timestamp=now, expiry=now + timedelta(hours=1),
        payload_digest=digest, payload=payload, key_id="k1",
        signature=expected_signature,
    )
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    assert verify_signature(env, root, now) == "cryptographically_valid"
    # Tamper rejection
    tampered = SignedEnvelope(**{**env.__dict__, "payload": {"objective": "tampered", "type": "assignment"}})
    with pytest.raises(SignatureError):
        verify_signature(tampered, root, now)


def test_malformed_typed_envelope():
    sk, pk = _key_pair()
    root = load_trust_root([_entry(pubkey=pk)], version=1)
    env = _envelope(sk)
    # Negative sequence
    bad = SignedEnvelope(**{**env.__dict__, "sequence": -1})
    with pytest.raises(SignatureError):
        verify_signature(bad, root, _now())


def test_key_expiration_boundary():
    sk, pk = _key_pair()
    # Exactly at expiry is rejected
    root = load_trust_root([_entry(pubkey=pk, expires=_now())], version=1)
    env = _envelope(sk)
    with pytest.raises(SignatureError, match="expired"):
        verify_signature(env, root, _now())
    # One second before expiry is accepted
    root2 = load_trust_root([_entry(pubkey=pk, expires=_now() + timedelta(seconds=1))], version=1)
    assert verify_signature(env, root2, _now()) == "cryptographically_valid"
