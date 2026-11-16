"""BRIDGE-02B.2.1 admission authorization, actor binding, SHA, evidence_id and snapshot tests.

Synthetic. Offline. Non-executing. execution_enabled remains False.

Groups follow the approved design (revision 2 as amended by revision 3):
A matrix and actor binding, B identity and duplicates (each case states the layer
it expects to reject at), C SHA fields, D evidence_id, E payload snapshot.
Every rejection asserts the exact reason string and logical no-mutation
(reservation rows, sqlite_master and user_version unchanged).
"""

import copy
import dataclasses
import hashlib
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import skippy.collaboration.offline_admission_gate as gate_module
from skippy.collaboration.offline_admission_gate import OfflineAdmissionGate
from skippy.collaboration.offline_signature_verifier import SignedEnvelope, _canonical_json
from test_offline_admission_gate import (
    ACTORS,
    VALID_SHA,
    ClearHalt,
    _actor_envelope,
    _key_pair,
    _now,
    _trust_root,
)
from test_sqlite_connection_lifecycle import _TrackingSqlite, _open

IMPL_SHA = "fedcba9876543210fedcba9876543210fedcba98"
OTHER_SHA = "89abcdef0123456789abcdef0123456789abcdef"

MATRIX = {
    ("chief", "chief", "jason", "assignment"): ("chief_authorization", "base_sha"),
    ("jason", "implementer", "skippy", "implementation"): (
        "chief_authorization",
        "implementation_sha",
    ),
    ("jason", "implementer", "skippy", "correction"): (
        "chief_authorization",
        "implementation_sha",
    ),
    ("skippy", "reviewer", "jason", "review"): ("skippy_review", "implementation_sha"),
}
ROUTE = {msg_type: (sender, recipient) for (sender, _r, recipient, msg_type) in MATRIX}
SHA_FIELD = {msg_type: sha for (_s, _r, _c, msg_type), (_k, sha) in MATRIX.items()}
TYPES = ("assignment", "implementation", "correction", "review")

MATRIX_REASON = "unauthorized sender/role/recipient/type combination"


class SpyEvidence:
    """Records every call; returns `result` (default verified) or raises when result is None."""

    def __init__(self, result="verified"):
        self.result = result
        self.calls = []

    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        self.calls.append((kind, assignment_id, bound_sha, evidence_id))
        for value in (kind, assignment_id, bound_sha, evidence_id):
            assert type(value) is str
        if self.result is None:
            raise RuntimeError("evidence backend failure")
        return self.result


def _payload(msg_type, **extra):
    payload = {"type": msg_type, "objective": "test", "evidence_id": "e1"}
    payload[SHA_FIELD.get(msg_type, "implementation_sha")] = (
        VALID_SHA if msg_type == "assignment" else IMPL_SHA
    )
    payload.update(extra)
    return payload


def _msg(msg_type, *, actor=None, payload=None, **overrides):
    """Matrix-valid signed message of msg_type unless overridden."""
    sender, recipient = ROUTE[msg_type]
    actor = actor or sender
    fields = dict(recipient=recipient, payload=payload or _payload(msg_type))
    fields.update(overrides)
    return _actor_envelope(actor, **fields)


def _gate(tmp_path, evidence=None, name="admit.db"):
    return OfflineAdmissionGate(
        tmp_path / name,
        _trust_root(_key_pair()[1]),
        evidence if evidence is not None else SpyEvidence(),
        ClearHalt(),
    )


def _db_state(path):
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute("SELECT * FROM reservations ORDER BY id").fetchall()
        master = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    return rows, master, version


def _assert_rejected(gate, envelope, reason, spy=None):
    before = _db_state(gate.database_path)
    calls_before = None if spy is None else len(spy.calls)
    result = gate.reserve(envelope, _now())
    assert (result.outcome, result.reason) == ("rejected", reason)
    assert _db_state(gate.database_path) == before
    if spy is not None:
        assert len(spy.calls) == calls_before, "evidence verifier must not be called"
    return result


def _rows(gate):
    return _db_state(gate.database_path)[0]


def _columns(gate):
    connection = sqlite3.connect(gate.database_path)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(r) for r in connection.execute("SELECT * FROM reservations ORDER BY id")]
    finally:
        connection.close()


# --- A. matrix and actor binding -------------------------------------------


@pytest.mark.parametrize("key", list(MATRIX))
def test_a1_each_matrix_row_reserved_in_own_stream(tmp_path, key):
    sender, role, recipient, msg_type = key
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg(msg_type, task_id=f"t-{msg_type}")
    result = gate.reserve(env, _now())
    assert (result.outcome, result.reason) == ("reserved", "ok")
    kind, sha_field = MATRIX[key]
    assert spy.calls == [(kind, "a1", env.payload[sha_field], "e1")]
    [row] = _columns(gate)
    assert (row["sender"], row["recipient"], row["role"]) == (sender, recipient, role)


def test_a2_one_stream_multiple_legitimate_actors(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    sequence = ["assignment", "implementation", "review", "correction", "review"]
    for index, msg_type in enumerate(sequence, start=1):
        env = _msg(msg_type, message_id=f"m{index}", idempotency_key=f"i{index}", sequence=index)
        result = gate.reserve(env, _now())
        assert (result.outcome, result.reason) == ("reserved", "ok"), msg_type
    rows = _columns(gate)
    assert [r["sequence"] for r in rows] == [1, 2, 3, 4, 5]
    assert {r["assignment_id"] for r in rows} == {"a1"}
    assert [(r["sender"], r["recipient"], r["role"]) for r in rows] == [
        ("chief", "jason", "chief"),
        ("jason", "skippy", "implementer"),
        ("skippy", "jason", "reviewer"),
        ("jason", "skippy", "implementer"),
        ("skippy", "jason", "reviewer"),
    ]


def _a3_cases():
    for actor in ("chief", "jason", "skippy"):
        for role in ("chief", "implementer", "reviewer"):
            for recipient in ("chief", "jason", "skippy", "bridge"):
                for msg_type in TYPES:
                    if (actor, role, recipient, msg_type) not in MATRIX:
                        yield actor, role, recipient, msg_type


def test_a3_every_tuple_outside_matrix_rejected_at_its_layer(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    cases = list(_a3_cases())
    assert len(cases) == 3 * 3 * 4 * 4 - 4
    signature_layer = matrix_layer = 0
    for actor, role, recipient, msg_type in cases:
        env = _msg(msg_type, actor=actor, role=role, recipient=recipient)
        if role != ACTORS[actor][1]:
            expected = "invalid signature: Role mismatch"
            signature_layer += 1
        else:
            expected = MATRIX_REASON
            matrix_layer += 1
        _assert_rejected(gate, env, expected, spy)
    assert spy.calls == []
    assert signature_layer == 3 * 2 * 4 * 4
    assert matrix_layer == 3 * 1 * 4 * 4 - 4
    assert _rows(gate) == []


def test_a3_sender_signed_with_another_actors_key_rejected_at_signature(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    # chief's key (k1) claiming to be jason/implementer
    env = _msg("implementation", actor="chief", sender="jason", role="implementer")
    _assert_rejected(gate, env, "invalid signature: Actor mismatch", spy)


@pytest.mark.parametrize("bad_type", ["correction_request", "unknown", 5, ["review"], {"t": "x"}])
def test_a4_unknown_or_non_string_type_rejected(tmp_path, bad_type):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation", payload=_payload("implementation", type=bad_type))
    _assert_rejected(gate, env, "unauthorized message type", spy)


def test_a5_chief_signed_implementation_rejected_by_matrix(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation", actor="chief", recipient="jason")
    _assert_rejected(gate, env, MATRIX_REASON, spy)
    env = _msg("implementation", actor="chief", recipient="skippy")
    _assert_rejected(gate, env, MATRIX_REASON, spy)


def test_a5_skippy_signed_assignment_rejected_by_matrix(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("assignment", actor="skippy")
    _assert_rejected(gate, env, MATRIX_REASON, spy)


@pytest.mark.parametrize("msg_type", TYPES)
def test_a6_matrix_valid_invalid_signature_rejected(tmp_path, msg_type):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = dataclasses.replace(_msg(msg_type), signature=b"\xff" * 64)
    _assert_rejected(gate, env, "invalid signature: Invalid signature", spy)


@pytest.mark.parametrize(
    "result, reason",
    [
        ("rejected", "evidence rejected"),
        ("unavailable", "evidence unavailable"),
        (None, "evidence verification failed closed"),
    ],
)
@pytest.mark.parametrize("msg_type", TYPES)
def test_a7_valid_signature_without_verified_evidence_rejected(tmp_path, msg_type, result, reason):
    spy = SpyEvidence(result)
    gate = _gate(tmp_path, spy)
    _assert_rejected(gate, _msg(msg_type), reason)
    assert len(spy.calls) == 1


# --- B. identity and duplicates (layer order) ------------------------------


def _reserve_ok(gate, env):
    result = gate.reserve(env, _now())
    assert (result.outcome, result.reason) == ("reserved", "ok")


def test_b1_matrix_invalid_replay_rejected_before_identity_lookup(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    _reserve_ok(gate, _msg("assignment", message_id="m1", idempotency_key="i1"))
    replay = _msg("assignment", actor="jason", message_id="m1", idempotency_key="i1")
    result = _assert_rejected(gate, replay, MATRIX_REASON, spy)  # layer 6
    assert result.reason != "conflicting identity"
    assert len(_rows(gate)) == 1


def _stream_with_implementation(gate):
    _reserve_ok(gate, _msg("assignment", message_id="m1", idempotency_key="i1", sequence=1))
    impl = _msg("implementation", message_id="m2", idempotency_key="i2", sequence=2)
    _reserve_ok(gate, impl)
    return impl


def test_b2_recipient_only_change_rejected_by_matrix_not_identity(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    _stream_with_implementation(gate)
    changed = _msg(
        "implementation", message_id="m2", idempotency_key="i2", sequence=2, recipient="chief"
    )
    result = _assert_rejected(gate, changed, MATRIX_REASON, spy)  # layer 6
    assert result.reason != "conflicting identity"


def test_b3_role_only_change_rejected_at_signature_not_identity(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    _stream_with_implementation(gate)
    changed = _msg(
        "implementation", message_id="m2", idempotency_key="i2", sequence=2, role="reviewer"
    )
    _assert_rejected(gate, changed, "invalid signature: Role mismatch", spy)  # layer 4


def test_b4_changed_sender_passing_all_layers_is_conflicting_identity(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    _stream_with_implementation(gate)
    review = _msg("review", message_id="m2", idempotency_key="i2", sequence=3)
    calls = len(spy.calls)
    _assert_rejected(gate, review, "conflicting identity")  # layer 10
    assert len(spy.calls) == calls + 1, "evidence verifier must have been reached"
    assert len(_rows(gate)) == 2


def test_b5_same_sender_different_content_is_conflicting_identity(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    _stream_with_implementation(gate)
    correction = _msg("correction", message_id="m3", idempotency_key="i2", sequence=3)
    _assert_rejected(gate, correction, "conflicting identity")
    assert len(spy.calls) == 3


def test_b6_exact_replay_after_later_message_is_duplicate_across_restart(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    impl = _stream_with_implementation(gate)
    _reserve_ok(gate, _msg("review", message_id="m3", idempotency_key="i3", sequence=3))
    rows = _rows(gate)
    assert gate.reserve(impl, _now()).outcome == "duplicate"
    restarted = _gate(tmp_path, spy)
    result = restarted.reserve(impl, _now())
    assert (result.outcome, result.reason) == ("duplicate", "identical reservation")
    assert _rows(gate) == rows


def test_b6_concurrent_exact_replay_one_reserved_one_duplicate(tmp_path):
    gate = _gate(tmp_path)
    _stream_with_implementation(gate)
    correction = _msg("correction", message_id="m3", idempotency_key="i3", sequence=3)
    results = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        results.append(gate.reserve(correction, _now()).outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["duplicate", "reserved"]
    assert len(_rows(gate)) == 3


def test_b7_valid_message_with_different_assignment_id_rejected(tmp_path):
    gate = _gate(tmp_path)
    _reserve_ok(gate, _msg("assignment", message_id="m1", idempotency_key="i1"))
    impl = _msg("implementation", message_id="m2", idempotency_key="i2", sequence=2,
                assignment_id="a2")
    _assert_rejected(gate, impl, "assignment binding mismatch")


def test_b8_rejected_evidence_reported_before_identity_collision(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    _reserve_ok(gate, _msg("assignment", message_id="m1", idempotency_key="i1"))
    spy.result = "rejected"
    colliding = _msg("implementation", message_id="m1", idempotency_key="i9", sequence=2)
    result = _assert_rejected(gate, colliding, "evidence rejected")  # layer 9
    assert result.reason != "conflicting identity"


@pytest.mark.parametrize("column, value", [("recipient", "bridge"), ("role", "implementer")])
def test_stored_recipient_and_role_are_part_of_duplicate_identity(tmp_path, column, value):
    gate = _gate(tmp_path)
    env = _msg("assignment")
    _reserve_ok(gate, env)
    connection = sqlite3.connect(gate.database_path)
    try:
        with connection:
            connection.execute(f"UPDATE reservations SET {column} = ?", (value,))
    finally:
        connection.close()
    _assert_rejected(gate, env, "conflicting identity")


def test_persisted_identity_fields_match_signed_envelope(tmp_path):
    gate = _gate(tmp_path)
    env = _msg("review", message_id="mr", idempotency_key="ir")
    _reserve_ok(gate, env)
    [row] = _columns(gate)
    assert row["sender"] == "skippy"
    assert row["recipient"] == "jason"
    assert row["role"] == "reviewer"
    assert row["payload_digest"] == env.payload_digest
    assert (row["message_id"], row["idempotency_key"], row["state"]) == ("mr", "ir", "RESERVED")


# --- C. SHA fields ----------------------------------------------------------

_BAD_SHA_VALUES = [
    ("int", 5),
    ("empty", ""),
    ("39 chars", VALID_SHA[:39]),
    ("41 chars", VALID_SHA + "0"),
    ("64 hex", "0" * 64),
    ("uppercase", VALID_SHA.upper()),
    ("trailing newline", VALID_SHA + "\n"),
    ("surrounding spaces", f" {VALID_SHA} "),
    ("non-hex", "g" * 40),
]


@pytest.mark.parametrize("msg_type", TYPES)
@pytest.mark.parametrize("label, value", _BAD_SHA_VALUES, ids=[v[0] for v in _BAD_SHA_VALUES])
def test_c_required_sha_invalid_values_rejected(tmp_path, msg_type, label, value):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    field = SHA_FIELD[msg_type]
    env = _msg(msg_type, payload=_payload(msg_type, **{field: value}))
    _assert_rejected(gate, env, f"invalid {field}", spy)


@pytest.mark.parametrize("msg_type", TYPES)
def test_c_required_sha_missing_rejected(tmp_path, msg_type):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    field = SHA_FIELD[msg_type]
    payload = _payload(msg_type)
    del payload[field]
    _assert_rejected(gate, _msg(msg_type, payload=payload), f"invalid {field}", spy)


@pytest.mark.parametrize("msg_type", TYPES)
def test_c_required_sha_none_rejected_at_snapshot(tmp_path, msg_type):
    # None is outside the canonical-JSON profile, so it fails at layer 3, not layer 8.
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg(msg_type)
    bad = dataclasses.replace(env, payload={**env.payload, SHA_FIELD[msg_type]: None})
    _assert_rejected(gate, bad, "malformed payload", spy)


@pytest.mark.parametrize("variant", ["alone", "equal", "different"])
@pytest.mark.parametrize("msg_type", TYPES)
def test_c1_top_level_sha_alias_rejected(tmp_path, msg_type, variant):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    field = SHA_FIELD[msg_type]
    payload = _payload(msg_type)
    if variant == "alone":
        payload["sha"] = payload.pop(field)
    elif variant == "equal":
        payload["sha"] = payload[field]
    else:
        payload["sha"] = OTHER_SHA
    _assert_rejected(gate, _msg(msg_type, payload=payload), "adapter sha alias not accepted", spy)


def test_c1_nested_sha_key_is_not_the_alias(tmp_path):
    # D4: only the top-level `sha` key is the adapter alias.
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation", payload=_payload("implementation", meta={"sha": OTHER_SHA}))
    _reserve_ok(gate, env)
    assert spy.calls[-1][2] == IMPL_SHA


@pytest.mark.parametrize(
    "msg_type, forbidden",
    [
        ("assignment", "implementation_sha"),
        ("implementation", "base_sha"),
        ("correction", "base_sha"),
        ("review", "base_sha"),
    ],
)
def test_c2_forbidden_sha_field_rejected(tmp_path, msg_type, forbidden):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg(msg_type, payload=_payload(msg_type, **{forbidden: OTHER_SHA}))
    _assert_rejected(gate, env, f"forbidden field: {forbidden}", spy)


@pytest.mark.parametrize("msg_type", TYPES)
def test_c3_evidence_receives_exact_bound_sha(tmp_path, msg_type):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg(msg_type)
    _reserve_ok(gate, env)
    [kind] = [k for (_s, _r, _c, t), (k, _f) in MATRIX.items() if t == msg_type]
    expected = VALID_SHA if msg_type == "assignment" else IMPL_SHA
    assert spy.calls == [(kind, "a1", expected, "e1")]
    assert expected != ""


# --- D. evidence_id ---------------------------------------------------------


@pytest.mark.parametrize(
    "value, reason",
    [
        ("<missing>", "missing or empty evidence_id"),
        (5, "missing or empty evidence_id"),
        ("", "missing or empty evidence_id"),
        ("   ", "missing or empty evidence_id"),
        (" e1", "evidence_id has surrounding whitespace"),
        ("e1 ", "evidence_id has surrounding whitespace"),
        ("e1\n", "evidence_id has surrounding whitespace"),
    ],
)
def test_d_evidence_id_strict(tmp_path, value, reason):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    payload = _payload("implementation")
    if value == "<missing>":
        del payload["evidence_id"]
    else:
        payload["evidence_id"] = value
    _assert_rejected(gate, _msg("implementation", payload=payload), reason, spy)


def test_d_evidence_id_passed_without_coercion(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    payload = _payload("implementation", evidence_id="E-1 x")
    _reserve_ok(gate, _msg("implementation", payload=payload))
    assert spy.calls[-1][3] == "E-1 x"


# --- E. payload snapshot ----------------------------------------------------


@pytest.fixture
def tracker(monkeypatch):
    tracked = _TrackingSqlite(sqlite3)
    monkeypatch.setattr(gate_module, "sqlite3", tracked)
    return tracked


def _assert_malformed(gate, envelope, spy, tracker):
    opened = len(tracker.opened)
    _assert_rejected_without_db_read(gate, envelope, "malformed payload", spy)
    # exactly one connection: the initial _block_if_unresolved() check
    assert len(tracker.opened) == opened + 1
    assert _open(tracker.opened) == []


def _assert_rejected_without_db_read(gate, envelope, reason, spy):
    before = _db_state(gate.database_path)
    calls = len(spy.calls)
    result = gate.reserve(envelope, _now())
    assert (result.outcome, result.reason) == ("rejected", reason)
    assert len(spy.calls) == calls
    assert _db_state(gate.database_path) == before


_OUT_OF_PROFILE = [
    ("float", 1.5),
    ("bool", True),
    ("none", None),
    ("bytes", b"\x00"),
    ("lone surrogate", "\ud800"),
]


@pytest.mark.parametrize("label, value", _OUT_OF_PROFILE, ids=[v[0] for v in _OUT_OF_PROFILE])
def test_e1_out_of_profile_payload_values_rejected(tmp_path, tracker, label, value):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation")
    bad = dataclasses.replace(env, payload={**env.payload, "extra": value})
    _assert_malformed(gate, bad, spy, tracker)


def test_e1_non_string_key_rejected(tmp_path, tracker):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation")
    bad = dataclasses.replace(env, payload={**env.payload, 7: "x"})
    _assert_malformed(gate, bad, spy, tracker)


def test_e2_self_referencing_payload_rejected(tmp_path, tracker):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation")
    payload = dict(env.payload)
    payload["self"] = payload
    _assert_malformed(gate, dataclasses.replace(env, payload=payload), spy, tracker)


class _GrowingPayload(dict):
    def items(self):
        for index, item in enumerate(dict.items(self)):
            if index == 0:
                self["zz_added_during_items"] = "x"
            yield item


class _PayloadFailure(Exception):
    pass


class _RaisingPayload(dict):
    def items(self):
        raise _PayloadFailure("items failed")


def test_e3_payload_changing_size_during_copy_rejected(tmp_path, tracker):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation")
    bad = dataclasses.replace(env, payload=_GrowingPayload(env.payload))
    _assert_malformed(gate, bad, spy, tracker)


def test_e4_payload_items_raising_custom_exception_rejected(tmp_path, tracker):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation")
    bad = dataclasses.replace(env, payload=_RaisingPayload(env.payload))
    _assert_malformed(gate, bad, spy, tracker)


class _FirstReadDiffers(dict):
    """Same size; the first items() call reports different values than were signed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = 0

    def items(self):
        self.reads += 1
        if self.reads == 1:
            return [(k, "tampered" if k == "objective" else v) for k, v in dict.items(self)]
        return dict.items(self)


def test_e5_copy_differing_from_signed_content_fails_signature(tmp_path, tracker):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation")
    bad = dataclasses.replace(env, payload=_FirstReadDiffers(env.payload))
    opened = len(tracker.opened)
    _assert_rejected_without_db_read(
        gate, bad, "invalid signature: payload_digest does not match canonical payload", spy
    )
    assert len(tracker.opened) == opened + 1
    assert bad.payload.reads == 1, "payload must be read exactly once (the snapshot)"


class _LyingGet(dict):
    """Serializes as the signed content but lies through get()/[] afterwards."""

    def get(self, key, default=None):
        if key == "implementation_sha":
            return OTHER_SHA
        if key == "evidence_id":
            return "forged"
        return dict.get(self, key, default)

    def __getitem__(self, key):
        return self.get(key)


def test_e5_later_reads_cannot_diverge_from_verified_copy(tmp_path):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation")
    lying = dataclasses.replace(env, payload=_LyingGet(env.payload))
    _reserve_ok(gate, lying)
    assert spy.calls == [("chief_authorization", "a1", IMPL_SHA, "e1")]


class _MutatingEvidence(SpyEvidence):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        result = super().verify(kind, assignment_id, bound_sha, evidence_id, now)
        self.payload["implementation_sha"] = OTHER_SHA
        self.payload["type"] = "review"
        return result


def test_e6_caller_mutation_after_snapshot_does_not_change_outcome(tmp_path):
    env = _msg("implementation")
    signed_payload = copy.deepcopy(env.payload)
    spy = _MutatingEvidence(env.payload)
    gate = _gate(tmp_path, spy)
    _reserve_ok(gate, env)
    assert env.payload != signed_payload, "the test must actually mutate the caller payload"
    assert spy.calls == [("chief_authorization", "a1", IMPL_SHA, "e1")]
    [row] = _columns(gate)
    assert row["payload_digest"] == hashlib.sha256(_canonical_json(signed_payload)).digest()
    assert (row["sender"], row["recipient"], row["role"]) == ("jason", "skippy", "implementer")


class _EnvelopeSubclass(SignedEnvelope):
    pass


@pytest.mark.parametrize("kind", ["subclass", "namespace", "dict"])
def test_e7_non_signed_envelope_input_rejected(tmp_path, tracker, kind):
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    env = _msg("implementation")
    fields = {f.name: getattr(env, f.name) for f in dataclasses.fields(env)}
    candidate = {
        "subclass": lambda: _EnvelopeSubclass(**fields),
        "namespace": lambda: SimpleNamespace(**fields),
        "dict": lambda: fields,
    }[kind]()
    _assert_malformed(gate, candidate, spy, tracker)


def test_e8_successful_reserve_leaves_caller_payload_untouched(tmp_path):
    gate = _gate(tmp_path)
    env = _msg("review")
    payload_object = env.payload
    before = copy.deepcopy(env.payload)
    _reserve_ok(gate, env)
    assert env.payload is payload_object
    assert env.payload == before


def test_e9_real_verifier_sees_copy_and_later_decisions_use_it(tmp_path, monkeypatch):
    env = _msg("implementation")
    caller_payload = env.payload
    seen = []
    real_verify = gate_module.verify_signature

    def spy_verify(envelope, trust_root, now):
        seen.append(envelope.payload)
        verdict = real_verify(envelope, trust_root, now)
        # After authentication, tamper with the caller's object; decisions must not see it.
        caller_payload["implementation_sha"] = OTHER_SHA
        caller_payload["evidence_id"] = "forged"
        return verdict

    monkeypatch.setattr(gate_module, "verify_signature", spy_verify)
    spy = SpyEvidence()
    gate = _gate(tmp_path, spy)
    _reserve_ok(gate, env)
    assert len(seen) == 1
    assert seen[0] is not caller_payload
    assert type(seen[0]) is dict
    assert spy.calls == [("chief_authorization", "a1", IMPL_SHA, "e1")]
