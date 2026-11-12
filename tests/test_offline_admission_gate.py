"""BRIDGE-02B.2.1 offline admission reservation tests."""

import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from nacl.signing import SigningKey

from skippy.collaboration.offline_admission_gate import (
    AdmissionError,
    OfflineAdmissionGate,
)
from skippy.collaboration.offline_signature_verifier import (
    KeyEntry,
    SignedEnvelope,
    _canonical_json,
    load_trust_root,
)


# Full 40-character lowercase Git SHA-1 used by the shared fixtures.
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"

# Synthetic actors: actor -> (key_id, permitted_role, seed). k1 is the original chief key.
ACTORS = {
    "chief": ("k1", "chief", b"\x01" * 32),
    "jason": ("k2", "implementer", b"\x02" * 32),
    "skippy": ("k3", "reviewer", b"\x03" * 32),
}


def _now():
    return datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _key_pair(seed: bytes = b"\x01" * 32):
    sk = SigningKey(seed)
    return sk, sk.verify_key.encode()


def _signer(actor: str) -> SigningKey:
    return SigningKey(ACTORS[actor][2])


class AllowEvidence:
    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        if not evidence_id or not evidence_id.strip():
            return "rejected"
        if not bound_sha or bound_sha != VALID_SHA:
            return "rejected"
        return "verified"


class RejectEvidence:
    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        return "rejected"


class ClearHalt:
    def read(self):
        return "clear"


class UnavailableHalt:
    def read(self):
        return "unavailable"


class RaisingHalt:
    def read(self):
        raise RuntimeError("probe failure")


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
        payload={
            "type": "assignment",
            "objective": "test",
            "evidence_id": "e1",
            "base_sha": VALID_SHA,
        },
        key_id="k1",
    )
    defaults.update(overrides)
    payload_bytes = _canonical_json(defaults["payload"])
    defaults["payload_digest"] = hashlib.sha256(payload_bytes).digest()
    env = SignedEnvelope(**defaults, signature=b"\x00" * 64)
    from skippy.collaboration.offline_signature_verifier import canonical_bytes

    sig = signing_key.sign(canonical_bytes(env)).signature
    return SignedEnvelope(**defaults, signature=sig)


def _actor_envelope(actor, **overrides):
    """Sign as a registered synthetic actor: sets sender, role and key_id from ACTORS."""
    key_id, role, _seed = ACTORS[actor]
    fields = dict(sender=actor, role=role, key_id=key_id)
    fields.update(overrides)
    return _envelope(_signer(actor), **fields)


def _trust_root(chief_pk):
    expires = _now() + timedelta(days=30)
    return load_trust_root(
        [
            KeyEntry("k1", "chief", "chief", chief_pk, "active", expires),
            KeyEntry("k2", "jason", "implementer", _signer("jason").verify_key.encode(), "active",
                     expires),
            KeyEntry("k3", "skippy", "reviewer", _signer("skippy").verify_key.encode(), "active",
                     expires),
        ],
        version=1,
    )


def _gate(tmp_path, sk_pk, evidence=None, halt=None):
    sk, pk = sk_pk
    root = _trust_root(pk)
    return OfflineAdmissionGate(
        tmp_path / "admit.db",
        root,
        evidence or AllowEvidence(),
        halt or ClearHalt(),
    )


def test_first_reservation(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    result = gate.reserve(_envelope(sk), _now())
    assert result.outcome == "reserved"


def test_duplicate_after_restart(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    env = _envelope(sk)
    assert gate.reserve(env, _now()).outcome == "reserved"
    gate2 = _gate(tmp_path, (sk, pk))
    assert gate2.reserve(env, _now()).outcome == "duplicate"


def test_sequence_must_start_at_one(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    result = gate.reserve(_envelope(sk, sequence=2), _now())
    assert result.outcome == "rejected"
    assert "sequence" in result.reason


def test_message_id_reuse_with_new_idempotency_key_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    assert gate.reserve(_envelope(sk), _now()).outcome == "reserved"
    other = _envelope(sk, message_id="m1", idempotency_key="i2", sender="chief")
    result = gate.reserve(other, _now())
    assert result.outcome == "rejected"
    assert result.reason == "conflicting identity"


def test_invalid_signature_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    env = _envelope(sk)
    bad = SignedEnvelope(**{**env.__dict__, "signature": b"\xff" * 64})
    result = gate.reserve(bad, _now())
    assert result.outcome == "rejected"


def test_missing_evidence_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk), evidence=RejectEvidence())
    env = _actor_envelope(
        "jason",
        recipient="skippy",
        payload={"type": "implementation", "implementation_sha": VALID_SHA, "evidence_id": "e1"},
    )
    result = gate.reserve(env, _now())
    assert result.outcome == "rejected"
    assert "evidence" in result.reason


def test_empty_evidence_id_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    env = _envelope(sk, payload={"type": "assignment", "objective": "test", "evidence_id": "   "})
    result = gate.reserve(env, _now())
    assert result.outcome == "rejected"
    assert "missing or empty evidence_id" in result.reason


def test_unavailable_halt_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk), halt=UnavailableHalt())
    result = gate.reserve(_envelope(sk), _now())
    assert result.outcome == "rejected"
    assert "HALT" in result.reason


def test_halt_probe_exception_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk), halt=RaisingHalt())
    result = gate.reserve(_envelope(sk), _now())
    assert result.outcome == "rejected"
    assert "HALT probe failed closed" in result.reason


def test_pending_blocks_after_restart(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    with sqlite3.connect(tmp_path / "admit.db") as c:
        c.execute(
            """
            INSERT INTO reservations (
                project_id, task_id, checkpoint_id, assignment_id,
                message_id, idempotency_key, sender, recipient, role, payload_digest,
                sequence, state, recorded_at
            ) VALUES ('QLA','t1','c1','a1','old','oldk','chief','jason','chief',?,1,'PENDING',
                      '2026-11-08T12:00:00')
            """,
            (b"\x00" * 32,),
        )
    with pytest.raises(AdmissionError, match="PENDING"):
        _gate(tmp_path, (sk, pk))


def test_concurrent_same_identity(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    env = _envelope(sk)
    results = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        results.append(gate.reserve(env, _now()).outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("reserved") == 1
    assert results.count("duplicate") == 1


def test_concurrent_conflicting_identity(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    env1 = _envelope(sk, message_id="m1", idempotency_key="i1")
    env2 = _envelope(sk, message_id="m1", idempotency_key="i2")  # conflicting on message_id
    results = []
    barrier = threading.Barrier(2)

    def attempt(env):
        barrier.wait()
        results.append(gate.reserve(env, _now()).outcome)

    threads = [
        threading.Thread(target=attempt, args=(env1,)),
        threading.Thread(target=attempt, args=(env2,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("reserved") == 1
    assert results.count("rejected") == 1


def test_split_key_collision_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    # Create two distinct reservations
    env_a = _envelope(sk, message_id="ma", idempotency_key="ia", sequence=1)
    assert gate.reserve(env_a, _now()).outcome == "reserved"
    env_b = _envelope(sk, message_id="mb", idempotency_key="ib", sequence=2)
    assert gate.reserve(env_b, _now()).outcome == "reserved"
    # Attempt that collides on both keys with different records
    collision = _envelope(sk, message_id="ma", idempotency_key="ib", sequence=3)
    result = gate.reserve(collision, _now())
    assert result.outcome == "rejected"
    assert "ambiguous" in result.reason or "conflicting" in result.reason


def test_init_connection_failure_raises(tmp_path, monkeypatch):
    sk, pk = _key_pair()

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("injected init failure")

    monkeypatch.setattr(sqlite3, "connect", boom)
    with pytest.raises(AdmissionError) as excinfo:
        root = load_trust_root(
            [KeyEntry("k1", "chief", "chief", pk, "active", _now() + timedelta(days=30))],
            version=1,
        )
        OfflineAdmissionGate(
            tmp_path / "admit.db",
            root,
            AllowEvidence(),
            ClearHalt(),
        )
    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, sqlite3.Error)


class _PragmaFailingConnection:
    """Real connection whose execute() raises on one PRAGMA; close is a recording mock.

    Initialization now runs BEGIN IMMEDIATE and schema reads before the WAL and
    synchronous PRAGMAs (revision 3, I2-I4), so a positional side_effect list no longer
    reaches the PRAGMAs. Failing by statement keeps these tests aimed at PRAGMA failure.
    """

    def __init__(self, real, failing_prefix, message):
        self._real = real
        self._failing_prefix = failing_prefix
        self._message = message
        self.close = MagicMock(side_effect=real.close)

    def execute(self, sql, *args):
        if " ".join(sql.split()).upper().startswith(self._failing_prefix):
            raise sqlite3.OperationalError(self._message)
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_pragma_first_failure_closes_connection(tmp_path, monkeypatch):
    sk, pk = _key_pair()
    real_connect = sqlite3.connect
    mock_conn = None

    def fake_connect(*args, **kwargs):
        nonlocal mock_conn
        mock_conn = _PragmaFailingConnection(
            real_connect(*args, **kwargs), "PRAGMA JOURNAL_MODE", "first pragma fail"
        )
        return mock_conn

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(AdmissionError) as excinfo:
        root = load_trust_root(
            [KeyEntry("k1", "chief", "chief", pk, "active", _now() + timedelta(days=30))],
            version=1,
        )
        OfflineAdmissionGate(
            tmp_path / "admit.db",
            root,
            AllowEvidence(),
            ClearHalt(),
        )
    assert excinfo.value.__cause__ is not None
    mock_conn.close.assert_called()


def test_pragma_second_failure_closes_connection(tmp_path, monkeypatch):
    sk, pk = _key_pair()
    real_connect = sqlite3.connect
    mock_conn = None

    # First PRAGMA succeeds, second fails
    def fake_connect(*args, **kwargs):
        nonlocal mock_conn
        mock_conn = _PragmaFailingConnection(
            real_connect(*args, **kwargs), "PRAGMA SYNCHRONOUS", "second pragma fail"
        )
        return mock_conn

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(AdmissionError) as excinfo:
        root = load_trust_root(
            [KeyEntry("k1", "chief", "chief", pk, "active", _now() + timedelta(days=30))],
            version=1,
        )
        OfflineAdmissionGate(
            tmp_path / "admit.db",
            root,
            AllowEvidence(),
            ClearHalt(),
        )
    assert excinfo.value.__cause__ is not None
    mock_conn.close.assert_called()


def test_connect_journal_mode_pragma_failure_closes_connection(tmp_path, monkeypatch):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = [sqlite3.OperationalError("first pragma fail"), None]
    mock_conn.close = MagicMock()

    def fake_connect(*args, **kwargs):
        return mock_conn

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(sqlite3.OperationalError, match="first pragma fail"):
        gate._connect()
    mock_conn.close.assert_called()


def test_connect_synchronous_pragma_failure_closes_connection(tmp_path, monkeypatch):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    mock_conn = MagicMock()
    # First PRAGMA succeeds, second fails
    mock_conn.execute.side_effect = [None, sqlite3.OperationalError("second pragma fail")]
    mock_conn.close = MagicMock()

    def fake_connect(*args, **kwargs):
        return mock_conn

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(sqlite3.OperationalError, match="second pragma fail"):
        gate._connect()
    mock_conn.close.assert_called()


def test_connect_pragma_failure_during_reserve_raises_admission_error(tmp_path, monkeypatch):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = [sqlite3.OperationalError("first pragma fail"), None]
    mock_conn.close = MagicMock()
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: mock_conn)
    with pytest.raises(AdmissionError) as excinfo:
        gate.reserve(_envelope(sk), _now())
    assert isinstance(excinfo.value.__cause__, sqlite3.Error)
    mock_conn.close.assert_called()


def test_storage_failure_raises(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    gate.database_path.chmod(0o444)
    try:
        with pytest.raises(AdmissionError):
            def boom():
                raise sqlite3.OperationalError("disk full")

            gate._connect = boom
            gate.reserve(_envelope(sk, message_id="m9", idempotency_key="i9"), _now())
    finally:
        gate.database_path.chmod(0o644)


def test_unauthorized_type_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    env = _envelope(sk, payload={"type": "unauthorized", "objective": "test", "evidence_id": "e1"})
    result = gate.reserve(env, _now())
    assert result.outcome == "rejected"
    assert "unauthorized message type" in result.reason


def test_assignment_requires_chief_evidence(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk), evidence=RejectEvidence())
    result = gate.reserve(_envelope(sk), _now())
    assert result.outcome == "rejected"
    assert "evidence" in result.reason


def test_full_identity_duplicate(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    env = _envelope(sk)
    assert gate.reserve(env, _now()).outcome == "reserved"
    assert gate.reserve(env, _now()).outcome == "duplicate"


def test_conflicting_identity_rejected(tmp_path):
    sk, pk = _key_pair()
    gate = _gate(tmp_path, (sk, pk))
    assert gate.reserve(_envelope(sk), _now()).outcome == "reserved"
    conflict = _envelope(sk, message_id="m1", idempotency_key="i1", sequence=2)
    result = gate.reserve(conflict, _now())
    assert result.outcome == "rejected"
    assert "conflicting identity" in result.reason
