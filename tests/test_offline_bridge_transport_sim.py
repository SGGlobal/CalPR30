"""BRIDGE-02B.2.2 offline transport fixture tests.

OFFLINE SYNTHETIC ONLY. Governing contract: J13.6. P3 covers the bounded fake
source, exact frame extraction and delivery identity. P4 covers the disposable
journal, outbox and continuation. P5 covers the fake poster and read-back.
execution_enabled=False. No network or credentials.
"""

from __future__ import annotations

import ast
import hashlib
import json
import multiprocessing
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from nacl.signing import SigningKey

import skippy.collaboration.offline_bridge_transport_sim as sim
from skippy.collaboration.offline_bridge_transport_sim import (
    CLOSE_FENCE,
    MAX_AUTHOR_BYTES,
    MAX_BODY_BYTES,
    MAX_FRAME_BYTES,
    MAX_PER_PAGE,
    MAX_TOKEN_BYTES,
    OPEN_FENCE,
    REPOSITORY,
    CollectedBatch,
    CommentObservation,
    CommentPage,
    ContradictoryObservations,
    ExtractedFrame,
    FrameRefusal,
    SourceRefusal,
    body_digest,
    classify_delivery,
    collect_observations,
    PostOutcome,
    acknowledgment_marker,
    create_fresh,
    extract_frame,
    frame_digest,
    normalize_observations,
    open_existing,
)
from skippy.collaboration.checkpoint_ledger import CheckpointLedger
from skippy.collaboration.offline_admission_gate import OfflineAdmissionGate
from skippy.collaboration.offline_authenticated_adapter import (
    OfflineAuthenticatedAdapter,
    provision_signed_receipts_store,
)
from skippy.collaboration.offline_bridge_ingress import deliver
from skippy.collaboration.offline_signature_verifier import (
    KeyEntry,
    SignedEnvelope,
    _canonical_json,
    canonical_bytes,
    load_trust_root,
)

TS = "2026-11-14T00:00:01Z"
TS2 = "2026-11-14T00:00:02Z"


def obs(comment_id=1, body=b"body", updated_at=TS, author="auditor"):
    return CommentObservation(comment_id, updated_at, author, body)


def page(observations, token=None, snapshot="snap-1"):
    return CommentPage(snapshot, tuple(observations), token)


class ScriptSource:
    """Immutable fake. ``pages`` maps the request token (None is the first page)."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def page(self, repository, thread, token):
        self.calls.append((repository, thread, token))
        item = self.pages[token]
        if callable(item):
            return item(repository, thread, token)
        return item


def fence(frame: bytes, *, prefix=b"", suffix=b"", close_lf=True) -> bytes:
    body = prefix + OPEN_FENCE + b"\n" + frame + b"\n" + CLOSE_FENCE
    if close_lf:
        body += b"\n"
    if suffix:
        body += suffix
    return body


def test_profile_constants_match_the_fixture_contract():
    assert REPOSITORY == "SGGlobal/QLA"
    assert sim.DEFAULT_THREADS == (23, 26)
    assert sim.MAX_PAGES == 4
    assert MAX_PER_PAGE == 25
    assert sim.MAX_OBSERVATIONS == 100
    assert MAX_BODY_BYTES == 131072
    assert MAX_AUTHOR_BYTES == 256
    assert MAX_TOKEN_BYTES == 128
    assert sim.MIN_FRAME_BYTES == 1 and MAX_FRAME_BYTES == 65536
    assert sim.execution_enabled is False


def test_invalid_scalars_sizes_and_page_shape_are_source_refusals():
    cases = [
        obs(comment_id=True),
        obs(comment_id=0),
        obs(comment_id=-1),
        obs(updated_at="2026-11-14T00:00:01.0Z"),
        obs(updated_at="2026-02-30T00:00:00Z"),
        obs(updated_at="2026-11-14t00:00:01Z"),
        obs(author="a" * (MAX_AUTHOR_BYTES + 1)),
        obs(author="bad\ud800"),
        obs(body=b"x" * (MAX_BODY_BYTES + 1)),
        CommentObservation(1, TS, "auditor", "not-bytes"),
    ]
    for observation in cases:
        with pytest.raises(SourceRefusal):
            normalize_observations((observation,))
    with pytest.raises(SourceRefusal):
        collect_observations(
            ScriptSource({None: CommentPage("snap", [obs()], None)}), REPOSITORY, 23,
        )
    with pytest.raises(SourceRefusal):
        collect_observations(
            ScriptSource({None: page([obs()])}), "other/repo", 23,
        )
    with pytest.raises(SourceRefusal):
        collect_observations(ScriptSource({None: page([obs()])}), REPOSITORY, True)
    oversized = page(tuple(obs(comment_id=i + 1) for i in range(MAX_PER_PAGE + 1)))
    source = ScriptSource({None: oversized})
    with pytest.raises(SourceRefusal):
        collect_observations(source, REPOSITORY, 23)
    assert source.calls == [(REPOSITORY, 23, None)]
    long_token = "t" * (MAX_TOKEN_BYTES + 1)
    source = ScriptSource({None: page([obs()], token=long_token)})
    with pytest.raises(SourceRefusal):
        collect_observations(source, REPOSITORY, 23)
    assert len(source.calls) == 1


def test_exact_duplicates_collapse_and_ordering_is_deterministic():
    # Page order is later-id first. Same body with a new author/timestamp collapses.
    raw = (
        obs(20, b"b", TS2, "z-author"),
        obs(3, b"a", TS2, "later-author"),
        obs(3, b"a", TS, "earlier-author"),
        obs(3, b"a", TS, "earlier-author"),
        obs(20, b"b", TS, "z-author"),
    )
    ordered = normalize_observations(raw)
    assert [(item.comment_id, item.updated_at, item.author, item.body) for item in ordered] == [
        (3, TS, "earlier-author", b"a"),
        (20, TS, "z-author", b"b"),
    ]
    source = ScriptSource({None: page(raw)})
    batch = collect_observations(source, REPOSITORY, 26)
    assert batch.observations == ordered
    assert batch.complete is True and batch.next_token is None
    assert batch.source_calls == 1 and source.calls == [(REPOSITORY, 26, None)]
    assert isinstance(batch, CollectedBatch)


def test_contradictory_bodies_hold_the_batch():
    source = ScriptSource({None: page((obs(7, b"one"), obs(7, b"two", TS2), obs(8, b"other")))})
    with pytest.raises(ContradictoryObservations) as caught:
        collect_observations(source, REPOSITORY, 23)
    assert caught.value.comment_id == 7
    assert source.calls == [(REPOSITORY, 23, None)]


def test_four_page_bound_is_incomplete_and_does_not_fetch_further():
    def make(token, nxt, comment_id):
        return page((obs(comment_id, f"b{comment_id}".encode()),), token=nxt, snapshot="snap-a")

    source = ScriptSource({
        None: make(None, "p2", 1),
        "p2": make("p2", "p3", 2),
        "p3": make("p3", "p4", 3),
        "p4": make("p4", "p5", 4),
        "p5": lambda *_args: (_ for _ in ()).throw(AssertionError("fifth page")),
    })
    batch = collect_observations(source, REPOSITORY, 23)
    assert [item.comment_id for item in batch.observations] == [1, 2, 3, 4]
    assert batch.complete is False and batch.next_token == "p5" and batch.snapshot_id == "snap-a"
    assert [call[2] for call in source.calls] == [None, "p2", "p3", "p4"]
    assert batch.source_calls == 4


def test_short_source_completes_without_claiming_a_further_page():
    source = ScriptSource({
        None: page((obs(2, b"second", TS2),), token="p2"),
        "p2": page((obs(1, b"first", TS),), token=None),
    })
    batch = collect_observations(source, REPOSITORY, 23)
    assert [item.comment_id for item in batch.observations] == [1, 2]
    assert batch.complete is True and batch.next_token is None
    assert [call[2] for call in source.calls] == [None, "p2"]


def test_repeated_token_and_mid_pass_snapshot_change_refuse():
    repeated = ScriptSource({None: page((obs(),), token="again"), "again": page((obs(2),), token="again")})
    with pytest.raises(SourceRefusal):
        collect_observations(repeated, REPOSITORY, 23)
    assert [call[2] for call in repeated.calls] == [None, "again"]
    changed = ScriptSource({
        None: page((obs(),), token="p2", snapshot="one"),
        "p2": page((obs(2),), snapshot="two"),
    })
    with pytest.raises(SourceRefusal):
        collect_observations(changed, REPOSITORY, 23)
    assert len(changed.calls) == 2


def test_other_snapshot_or_failing_resume_restarts_at_the_first_page():
    source = ScriptSource({
        "stale": page((obs(9, b"stale"),), snapshot="other"),
        None: page((obs(1, b"fresh"), obs(1, b"fresh")), snapshot="new"),
    })
    batch = collect_observations(
        source, REPOSITORY, 23, start_token="stale", stored_snapshot="kept",
    )
    assert batch.restarted is True
    assert [item.comment_id for item in batch.observations] == [1]
    assert batch.snapshot_id == "new" and batch.complete is True
    assert [call[2] for call in source.calls] == ["stale", None]
    assert batch.source_calls == 2

    def boom(repository, thread, token):
        raise RuntimeError(f"unavailable:{token}")

    failing = ScriptSource({"bad": boom, None: page((obs(4, b"recovered"),))})
    batch = collect_observations(
        failing, REPOSITORY, 23, start_token="bad", stored_snapshot="snap-1",
    )
    assert batch.restarted is True
    assert batch.observations[0].body == b"recovered"
    assert [call[2] for call in failing.calls] == ["bad", None]

    fresh = ScriptSource({None: boom})
    with pytest.raises(RuntimeError, match="unavailable:None"):
        collect_observations(fresh, REPOSITORY, 23)
    assert fresh.calls == [(REPOSITORY, 23, None)]


def test_extract_keeps_the_original_slice_and_accepts_65536():
    frame = "{\"a\": \"caf\u00e9\"}\n".encode("utf-8")
    body = fence(frame, prefix=b"note\n", suffix=b"tail\n")
    extracted = extract_frame(body)
    assert isinstance(extracted, ExtractedFrame)
    assert extracted.frame == frame
    assert body[extracted.start:extracted.end] == frame
    assert frame_digest(extracted.frame) == hashlib.sha256(frame).digest()
    bare = OPEN_FENCE + b"\n" + frame + b"\n" + CLOSE_FENCE
    assert extract_frame(bare).frame == frame
    big = b"x" * MAX_FRAME_BYTES
    assert extract_frame(fence(big)).frame == big
    refused = extract_frame(fence(b"x" * (MAX_FRAME_BYTES + 1)))
    assert isinstance(refused, FrameRefusal) and refused.reason == "frame-too-large"


def test_fence_refusals_do_not_rewrite_bytes():
    original = b"```qla-bridge-frame-v1\r\n{}\r\n```\r\n"
    refused = extract_frame(original)
    assert refused.reason == "no-frame"
    assert original == b"```qla-bridge-frame-v1\r\n{}\r\n```\r\n"
    cases = {
        "no-frame": b"just commentary\n",
        "fence": fence(b"{}") + b"```\nmore\n",
        "fence-indent": b" ```\n" + fence(b"{}"),
        "fence-tag": b"```python\n{}\n```\n",
        "empty-frame": fence(b""),
        "bom": b"\xef\xbb\xbf" + fence(b"{}"),
        "utf8": b"\xff\xfe" + fence(b"{}"),
        "not-bytes": "not-bytes",
    }
    # Two blocks and a tag variant are fence refusals. Empty slice is empty-frame.
    assert extract_frame(fence(b"{}") + fence(b"{}")).reason == "fence"
    assert extract_frame(b"```qla-bridge-frame-v1 extra\n{}\n```\n").reason == "no-frame"
    reasons = {name: extract_frame(body).reason for name, body in cases.items()}
    assert reasons["no-frame"] == "no-frame"
    assert reasons["fence"] == "fence"
    assert reasons["fence-indent"] == "fence"
    assert reasons["fence-tag"] == "no-frame"
    assert reasons["empty-frame"] == "empty-frame"
    assert reasons["bom"] == "bom"
    assert reasons["utf8"] == "utf8"
    assert reasons["not-bytes"] == "not-bytes"


def test_delivery_identity_pins_body_bytes_not_canonical_json():
    first = fence(b'{"a":1}')
    same = bytes(first)
    whitespace = fence(b'{ "a" : 1 }')
    resigned = fence(b'{"a":1,"message_id":"other"}')
    assert classify_delivery(None, first) == "new"
    pinned = body_digest(first)
    assert classify_delivery(pinned, same) == "duplicate"
    assert classify_delivery(pinned, whitespace) == "edit"
    assert classify_delivery(pinned, resigned) == "edit"
    assert body_digest(first) == hashlib.sha256(first).digest()
    assert body_digest(whitespace) != pinned
    with pytest.raises(sim.TransportFixtureError):
        classify_delivery(b"short", first)


def test_source_module_has_no_network_admission_or_retry_api():
    source = Path(sim.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__", "hashlib", "re", "dataclasses", "json", "os", "sqlite3", "pathlib",
        "threading", "skippy",
    }
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not any(word in name.lower() for name in names for word in ("retry", "repost", "recover", "resume"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    text = ast.unparse(tree).lower()
    for needle in ("socket", "urllib", "http.client", "github_http", "urlopen", "credential"):
        assert needle not in text
    assert sim.execution_enabled is False


# --- P4 journal, outbox and continuation ---

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
PROJECT, TASK, CHECKPOINT, ASSIGNMENT = "QLA", "task-transport", "ckpt-transport", "assign-transport"


class _Interrupt(BaseException):
    """Stand-in for an interrupt. Not an Exception."""


class HaltCheck:
    def __init__(self, states=("clear",)):
        self.states = tuple(states)
        self.calls = 0

    def __call__(self):
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        if state == "raise":
            raise RuntimeError("halt probe")
        return state == "halted"


class CountingAdapter:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def accept_signed(self, envelope, *, now=None):
        self.calls += 1
        return self.inner.accept_signed(envelope, now=now)


class FixtureEvidence:
    def verify(self, kind, assignment_id, bound_sha, evidence_id, now):
        if not str(evidence_id).startswith("fixture-") or assignment_id != ASSIGNMENT:
            return "rejected"
        return "verified"


class FixtureProbe:
    def read(self):
        return "clear"


def make_keys():
    actors = {
        "chief": ("chief", b"\x11" * 32),
        "jason": ("implementer", b"\x12" * 32),
        "skippy": ("reviewer", b"\x13" * 32),
    }
    signers, entries = {}, []
    for actor, (role, seed) in actors.items():
        key_id = "test-b2b2-transport-" + actor
        signer = SigningKey(seed)
        signers[actor] = (key_id, signer)
        entries.append(KeyEntry(
            key_id, actor, role, signer.verify_key.encode(), "active", NOW + timedelta(days=30),
        ))
    return load_trust_root(entries, version=1), signers


IMPL_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def make_envelope(signers, *, tag="a", sequence=1, mid=None, idem=None, kind="assignment"):
    if kind == "assignment":
        sender, role, recipient = "chief", "chief", "jason"
        payload = {
            "type": "assignment", "objective": "synthetic transport", "evidence_id": "fixture-assign",
            "base_sha": BASE_SHA,
        }
    else:
        sender, role, recipient = "jason", "implementer", "skippy"
        payload = {
            "type": "implementation", "evidence_id": "fixture-impl", "implementation_sha": IMPL_SHA,
        }
    key_id, signer = signers[sender]
    unsigned = SignedEnvelope(
        schema_version=1, sender=sender, recipient=recipient, role=role,
        project_id=PROJECT, task_id=TASK, checkpoint_id=CHECKPOINT, assignment_id=ASSIGNMENT,
        message_id=mid or f"tr-{tag}", idempotency_key=idem or f"tr-idem-{tag}",
        sequence=sequence, timestamp=NOW - timedelta(seconds=60), expiry=NOW + timedelta(hours=1),
        payload_digest=hashlib.sha256(_canonical_json(payload)).digest(), payload=payload,
        key_id=key_id, signature=b"\x00" * 64,
    )
    return replace(unsigned, signature=signer.sign(canonical_bytes(unsigned)).signature)


def envelope_frame(env) -> bytes:
    obj = {
        "schema_version": env.schema_version, "sender": env.sender, "recipient": env.recipient,
        "role": env.role, "project_id": env.project_id, "task_id": env.task_id,
        "checkpoint_id": env.checkpoint_id, "assignment_id": env.assignment_id,
        "message_id": env.message_id, "idempotency_key": env.idempotency_key,
        "sequence": env.sequence, "timestamp": env.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry": env.expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload_digest": env.payload_digest.hex(), "payload": env.payload,
        "key_id": env.key_id, "signature": env.signature.hex(),
    }
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return fence(raw)


class AdmissionStores:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.receipts = root / "receipts.db"
        self.admission = root / "admission.db"
        self.ledger_path = root / "ledger.db"
        provision_signed_receipts_store(self.receipts)
        self.ledger = CheckpointLedger(self.ledger_path)
        self.trust, self.signers = make_keys()
        self.gate = OfflineAdmissionGate(self.admission, self.trust, FixtureEvidence(), FixtureProbe())

    def adapter(self):
        return OfflineAuthenticatedAdapter.open_signed(
            self.ledger, self.receipts, admission_gate=self.gate, halt_check=HaltCheck(),
            trusted_fixture=True,
        )


def _ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)


def ledger_rows(stores):
    connection = _ro(stores.ledger_path)
    try:
        return connection.execute(
            "SELECT revision, message_id, actor FROM events ORDER BY revision"
        ).fetchall()
    finally:
        connection.close()


def intent_states(stores):
    connection = _ro(stores.receipts)
    try:
        return connection.execute("SELECT state FROM intent_journal ORDER BY intent_id").fetchall()
    finally:
        connection.close()


def test_create_fresh_refuses_an_existing_file_and_reopen_cannot_poll(tmp_path):
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    with pytest.raises(sim.TransportFixtureError):
        create_fresh(session.path, REPOSITORY, (23, 26))
    inspected = open_existing(session.path)
    assert inspected.blocking is False and inspected.allowed_threads == (23, 26)
    assert not hasattr(inspected, "poll_once") and not hasattr(inspected, "post_once")
    with pytest.raises(sim.InspectionError):
        open_existing(tmp_path / "missing.db")


def test_assignment_round_trip_then_duplicate_skips_deliver(tmp_path):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    body = envelope_frame(make_envelope(stores.signers))
    source = ScriptSource({None: page((obs(1, body, author="not-a-signer"),))})
    adapter = CountingAdapter(stores.adapter())
    report = session.poll_once(source, adapter, NOW, thread=23)
    assert report.status == "COMPLETE" and report.manual_required is False
    assert report.deliver_calls == 1 and adapter.calls == 1 and report.source_calls == 1
    assert report.dispositions[0].kind == "ADMITTED"
    assert report.dispositions[0].author == "not-a-signer"
    assert report.dispositions[0].adapter_calls == 1
    inspected = open_existing(session.path)
    assert inspected.blocking is False and len(inspected.outbox) == 1
    assert inspected.outbox[0][3] == "NOT_ATTEMPTED" and inspected.outbox[0][5] == "NOT_CHECKED"
    assert inspected.continuation == ((23, "snap-1", None),)
    assert ledger_rows(stores) == [(1, "tr-a", "chief")]
    assert intent_states(stores) == [("ACCEPTED",)]
    again = session.poll_once(source, adapter, NOW, thread=23)
    assert again.deliver_calls == 0 and adapter.calls == 1
    assert again.dispositions[0].kind == "DUPLICATE"
    assert ledger_rows(stores) == [(1, "tr-a", "chief")]


def test_same_frame_under_another_comment_is_not_a_second_admission(tmp_path):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    body = envelope_frame(make_envelope(stores.signers))
    source = ScriptSource({None: page((obs(1, body, TS), obs(2, body, TS2)))})
    adapter = CountingAdapter(stores.adapter())
    report = session.poll_once(source, adapter, NOW, thread=23)
    assert [item.kind for item in report.dispositions] == ["ADMITTED", "REJECTED"]
    assert report.deliver_calls == 2 and adapter.calls == 2 and report.manual_required is False
    assert intent_states(stores) == [("ACCEPTED",)]
    assert len(ledger_rows(stores)) == 1


def test_changed_body_is_held_before_deliver(tmp_path, monkeypatch):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    original = envelope_frame(make_envelope(stores.signers))
    adapter = CountingAdapter(stores.adapter())
    first = session.poll_once(ScriptSource({None: page((obs(1, original),))}), adapter, NOW, thread=23)
    assert first.dispositions[0].kind == "ADMITTED"
    pinned = open_existing(session.path).deliveries[0][3]
    calls = {"n": 0}

    def forbidden(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("deliver")

    monkeypatch.setattr(sim, "deliver_frame", forbidden)
    edited = ScriptSource({None: page((obs(1, fence(b'{ "a" : 1 }'), TS2),))})
    report = session.poll_once(edited, adapter, NOW, thread=23)
    assert report.manual_required is True and report.deliver_calls == 0 and calls["n"] == 0
    assert report.dispositions[0].kind == "EDIT_HELD"
    kept = open_existing(session.path)
    assert kept.deliveries[0][3] == pinned and "ADMITTED" in kept.deliveries[0][6]
    assert session.poll_once(edited, adapter, NOW, thread=23).deliver_calls == 0


def test_off_list_and_source_exception_do_not_look_like_an_empty_success(tmp_path):
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    source = ScriptSource({None: page((obs(),))})
    report = session.poll_once(source, object(), NOW, thread=99)
    assert report.status == "SOURCE_REFUSED" and report.deliver_calls == 0 and source.calls == []
    fresh = create_fresh(tmp_path / "other.db", REPOSITORY, (23, 26))

    def boom(*_args):
        raise RuntimeError("source down")

    failing = ScriptSource({None: boom})
    report = fresh.poll_once(failing, object(), NOW, thread=23)
    assert report.status == "MANUAL" and report.complete is False
    assert report.deliver_calls == 0 and report.dispositions == () and report.source_calls == 1


def test_malformed_and_fence_refusal_make_zero_adapter_calls(tmp_path):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    source = ScriptSource({None: page((obs(1, fence(b"{}"), TS), obs(2, b"commentary only", TS2)))})
    adapter = CountingAdapter(stores.adapter())
    report = session.poll_once(source, adapter, NOW, thread=23)
    assert [item.kind for item in report.dispositions] == ["MALFORMED", "TRANSPORT_REFUSED"]
    assert report.deliver_calls == 1 and adapter.calls == 0 and report.manual_required is False
    inspected = open_existing(session.path)
    assert inspected.outbox == ()
    assert inspected.deliveries[1][4] is None
    assert inspected.deliveries[1][3] == body_digest(b"commentary only")
    assert "FRAME_REFUSED" in inspected.deliveries[1][6]


def test_id_tie_admits_the_lower_sequence_first(tmp_path):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    first = envelope_frame(make_envelope(
        stores.signers, tag="1", sequence=1, mid="tr-1", idem="idem-1", kind="assignment",
    ))
    second = envelope_frame(make_envelope(
        stores.signers, tag="2", sequence=2, mid="tr-2", idem="idem-2", kind="implementation",
    ))
    source = ScriptSource({None: page((obs(9, second, TS), obs(4, first, TS)))})
    adapter = CountingAdapter(stores.adapter())
    report = session.poll_once(source, adapter, NOW, thread=23)
    assert [item.comment_id for item in report.dispositions] == [4, 9]
    assert [item.kind for item in report.dispositions] == ["ADMITTED", "ADMITTED"]
    assert [row[1] for row in ledger_rows(stores)] == ["tr-1", "tr-2"]


def test_omitted_comment_does_not_erase_a_recorded_admission(tmp_path):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    body = envelope_frame(make_envelope(stores.signers))
    session.poll_once(ScriptSource({None: page((obs(1, body),))}), stores.adapter(), NOW, thread=23)
    before = open_existing(session.path)
    report = session.poll_once(
        ScriptSource({None: page((), snapshot="snap-2")}), stores.adapter(), NOW, thread=23,
    )
    assert report.deliver_calls == 0 and report.status == "COMPLETE"
    after = open_existing(session.path)
    assert after.deliveries == before.deliveries and after.outbox == before.outbox
    assert len(ledger_rows(stores)) == 1 and after.continuation[0][1] == "snap-2"


def test_continuation_records_the_four_page_bound(tmp_path):
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    pages, token = {}, None
    for index, nxt in enumerate(("p2", "p3", "p4", "p5")):
        pages[token] = page((obs(index + 1, b"no-frame-" + str(index).encode()),), token=nxt)
        token = nxt
    report = session.poll_once(ScriptSource(pages), object(), NOW, thread=26)
    assert report.status == "INCOMPLETE" and report.continuation_token == "p5"
    assert report.deliver_calls == 0 and report.manual_required is False
    inspected = open_existing(session.path)
    assert inspected.continuation == ((26, "snap-1", "p5"),)
    assert len(inspected.deliveries) == 4


def test_prepared_commit_seams_do_not_deliver(tmp_path, monkeypatch):
    real = sim.commit_prepared
    stores = AdmissionStores(tmp_path / "before" / "adm")
    session = create_fresh(tmp_path / "before" / "transport.db", REPOSITORY, (23, 26))
    body = envelope_frame(make_envelope(stores.signers))
    monkeypatch.setattr(
        sim, "commit_prepared", lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("before")),
    )
    report = session.poll_once(ScriptSource({None: page((obs(1, body),))}), stores.adapter(), NOW, thread=23)
    assert report.deliver_calls == 0 and report.manual_required is True
    assert open_existing(session.path).deliveries == ()

    def after(connection):
        real(connection)
        raise sqlite3.OperationalError("after")

    monkeypatch.setattr(sim, "commit_prepared", after)
    later_stores = AdmissionStores(tmp_path / "after" / "adm")
    later = create_fresh(tmp_path / "after" / "transport.db", REPOSITORY, (23, 26))
    adapter = CountingAdapter(later_stores.adapter())
    signed = envelope_frame(make_envelope(later_stores.signers))
    report = later.poll_once(ScriptSource({None: page((obs(1, signed),))}), adapter, NOW, thread=23)
    assert report.deliver_calls == 0 and adapter.calls == 0
    inspected = open_existing(later.path)
    assert inspected.deliveries[0][5] == "PREPARED" and inspected.blocking is True
    assert later.poll_once(ScriptSource({None: page(())}), adapter, NOW, thread=23).source_calls == 0


def test_final_outbox_and_continuation_commit_seams(tmp_path, monkeypatch):
    seams = ("commit_final", "commit_outbox", "commit_continuation", "commit_prepared")
    originals = {name: getattr(sim, name) for name in seams}

    def run(name, seam, mode):
        for key, fn in originals.items():
            monkeypatch.setattr(sim, key, fn)
        root = tmp_path / name
        stores = AdmissionStores(root / "adm")
        session = create_fresh(root / "transport.db", REPOSITORY, (23, 26))
        real = originals[seam]
        if mode == "before":
            def wrapped(_connection, _real=real):
                raise sqlite3.OperationalError("before")
        else:
            def wrapped(connection, _real=real):
                _real(connection)
                raise sqlite3.OperationalError("after")
        monkeypatch.setattr(sim, seam, wrapped)
        adapter = CountingAdapter(stores.adapter())
        body = envelope_frame(make_envelope(stores.signers))
        report = session.poll_once(ScriptSource({None: page((obs(1, body),))}), adapter, NOW, thread=23)
        return session, adapter, report, open_existing(session.path)

    session, adapter, report, inspected = run("final-before", "commit_final", "before")
    assert report.deliver_calls == 1 and adapter.calls == 1 and report.manual_required is True
    assert inspected.deliveries[0][5] == "PREPARED"
    assert session.poll_once(ScriptSource({None: page(())}), adapter, NOW, thread=23).deliver_calls == 0

    session, _adapter, report, inspected = run("final-after", "commit_final", "after")
    assert inspected.deliveries[0][5] == "FINAL" and report.manual_required is True and report.complete is False
    assert session.poll_once(ScriptSource({None: page(())}), _adapter, NOW, thread=23).source_calls == 0

    _session, _adapter, report, inspected = run("out-before", "commit_outbox", "before")
    assert report.deliver_calls == 1 and inspected.outbox == ()
    assert "NOT_CONFIRMED" in inspected.deliveries[0][6] and inspected.blocking is True

    _session, _adapter, report, inspected = run("out-after", "commit_outbox", "after")
    assert len(inspected.outbox) == 1 and "NOT_CONFIRMED" in inspected.deliveries[0][6]
    assert inspected.blocking is True

    _session, _adapter, report, inspected = run("cont-before", "commit_continuation", "before")
    assert report.dispositions[0].kind == "ADMITTED" and report.manual_required is True
    assert inspected.continuation == () and report.complete is False

    _session, _adapter, report, inspected = run("cont-after", "commit_continuation", "after")
    assert inspected.continuation[0][2] is None and report.manual_required is True and report.complete is False


def test_interrupt_during_deliver_leaves_prepared(tmp_path, monkeypatch):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    body = envelope_frame(make_envelope(stores.signers))

    def interrupt(*_args, **_kwargs):
        raise _Interrupt("stop")

    monkeypatch.setattr(sim, "deliver_frame", interrupt)
    with pytest.raises(_Interrupt):
        session.poll_once(ScriptSource({None: page((obs(1, body),))}), stores.adapter(), NOW, thread=23)
    inspected = open_existing(session.path)
    assert inspected.deliveries[0][5] == "PREPARED" and inspected.blocking is True
    assert session.poll_once(ScriptSource({None: page(())}), object(), NOW, thread=23).deliver_calls == 0


def test_uncertainty_after_a_real_ledger_write_blocks_the_next_poll(tmp_path, monkeypatch):
    """Whole-call ledger wrapper: it delegates, then raises. Not an internal COMMIT seam."""
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    inner = stores.adapter()
    real = inner.ledger.record_event

    def delegate_then_raise(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("after ledger write")

    monkeypatch.setattr(inner.ledger, "record_event", delegate_then_raise)
    adapter = CountingAdapter(inner)
    body = envelope_frame(make_envelope(stores.signers))
    report = session.poll_once(ScriptSource({None: page((obs(1, body),))}), adapter, NOW, thread=23)
    assert report.dispositions[0].kind == "UNCERTAIN" and report.manual_required is True
    assert adapter.calls == 1 and len(ledger_rows(stores)) == 1
    assert open_existing(session.path).blocking is True
    follow = session.poll_once(ScriptSource({None: page((obs(2, b"later"),))}), adapter, NOW, thread=23)
    assert follow.deliver_calls == 0 and follow.source_calls == 0 and adapter.calls == 1


# --- P5 fake post and read-back ------------------------------------------------

class FakePoster:
    def __init__(self, result=PostOutcome("POSTED", 4242), exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def post(self, repository, thread, body):
        self.calls.append((repository, thread, body))
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeReader:
    def __init__(self, body=None, exc=None):
        self.body = body
        self.exc = exc
        self.calls = []

    def read(self, key):
        self.calls.append(key)
        if self.exc is not None:
            raise self.exc
        return self.body


def admit_one(tmp_path):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    body = envelope_frame(make_envelope(stores.signers))
    session.poll_once(ScriptSource({None: page((obs(1, body),))}), stores.adapter(), NOW, thread=23)
    inspected = open_existing(session.path)
    return session, stores, inspected.outbox[0][0], bytes(inspected.outbox[0][2]), inspected.deliveries[0][6]


def test_posted_not_posted_and_unknown_are_not_reposted(tmp_path):
    session, stores, key, digest, admission = admit_one(tmp_path)
    marker = acknowledgment_marker(digest)
    poster = FakePoster()
    report = session.post_once(poster)
    assert report.outcome == "POSTED" and report.unknown is False and report.poster_calls == 1
    assert report.external_id == 4242 and poster.calls[0][0] == REPOSITORY
    assert poster.calls[0][1] == 23 and marker in poster.calls[0][2]
    inspected = open_existing(session.path)
    assert inspected.outbox[0][3] == "POSTED" and inspected.outbox[0][4] == 4242
    assert inspected.deliveries[0][6] == admission
    assert len(ledger_rows(stores)) == 1
    again = session.post_once(poster)
    assert again.outcome == "NOT_ATTEMPTED" and again.poster_calls == 0 and len(poster.calls) == 1

    session, _stores, _key, _digest, _admission = admit_one(tmp_path / "refuse")
    poster = FakePoster(PostOutcome("NOT_POSTED"))
    assert session.post_once(poster).outcome == "NOT_POSTED"
    assert session.post_once(poster).poster_calls == 0 and len(poster.calls) == 1
    assert open_existing(session.path).outbox[0][3] == "NOT_POSTED"

    session, _stores, _key, _digest, _admission = admit_one(tmp_path / "raise")
    poster = FakePoster(exc=RuntimeError("reset"))
    report = session.post_once(poster)
    assert report.outcome == "POST_UNKNOWN" and report.poster_calls == 1 and len(poster.calls) == 1
    assert session.post_once(poster).poster_calls == 0 and len(poster.calls) == 1
    assert open_existing(session.path).outbox[0][3] == "POST_UNKNOWN"


def test_invalid_poster_returns_are_post_unknown(tmp_path):
    cases = (None, PostOutcome("POSTED", True), PostOutcome("POSTED", 0), PostOutcome("MAYBE"))
    for index, result in enumerate(cases):
        session, _stores, _key, _digest, _admission = admit_one(tmp_path / str(index))
        poster = FakePoster(result)
        report = session.post_once(poster)
        assert report.outcome == "POST_UNKNOWN" and report.poster_calls == 1
        assert open_existing(session.path).outbox[0][3] == "POST_UNKNOWN"
        assert open_existing(session.path).outbox[0][4] is None


def test_post_attempt_and_result_commit_seams(tmp_path, monkeypatch):
    originals = {
        "commit_post_attempt": sim.commit_post_attempt,
        "commit_post_result": sim.commit_post_result,
    }

    def run(name, seam, mode):
        for key, fn in originals.items():
            monkeypatch.setattr(sim, key, fn)
        session, _stores, key, _digest, _admission = admit_one(tmp_path / name)
        real = originals[seam]
        if mode == "before":
            def wrapped(_connection, _real=real):
                raise sqlite3.OperationalError("before")
        else:
            def wrapped(connection, _real=real):
                _real(connection)
                raise sqlite3.OperationalError("after")
        monkeypatch.setattr(sim, seam, wrapped)
        poster = FakePoster()
        report = session.post_once(poster)
        return session, poster, report, open_existing(session.path)

    session, poster, report, inspected = run("attempt-before", "commit_post_attempt", "before")
    assert report.unknown is True and report.poster_calls == 0 and poster.calls == []
    assert inspected.outbox[0][3] == "NOT_ATTEMPTED"
    assert session.post_once(poster).poster_calls == 0

    session, poster, report, inspected = run("attempt-after", "commit_post_attempt", "after")
    assert report.unknown is True and poster.calls == []
    assert inspected.outbox[0][3] == "ATTEMPTING"
    assert session.post_once(poster).poster_calls == 0 and session.post_once(poster).unknown is True

    session, poster, report, inspected = run("result-before", "commit_post_result", "before")
    assert report.unknown is True and report.outcome is None and len(poster.calls) == 1
    assert inspected.outbox[0][3] == "ATTEMPTING"
    assert session.post_once(poster).poster_calls == 0 and len(poster.calls) == 1

    session, poster, report, inspected = run("result-after", "commit_post_result", "after")
    assert report.unknown is True and report.outcome is None
    assert inspected.outbox[0][3] == "POSTED"
    assert session.post_once(poster).poster_calls == 0 and len(poster.calls) == 1


def test_poster_interrupt_leaves_attempting(tmp_path, monkeypatch):
    session, _stores, _key, _digest, _admission = admit_one(tmp_path)

    class Stop(BaseException):
        pass

    def interrupt(repository, thread, body):
        raise Stop("stop")

    poster = FakePoster()
    poster.post = interrupt
    with pytest.raises(Stop):
        session.post_once(poster)
    assert open_existing(session.path).outbox[0][3] == "ATTEMPTING"
    follow = session.post_once(FakePoster())
    assert follow.unknown is True and follow.poster_calls == 0


def test_read_back_present_absent_and_error_do_not_repost(tmp_path):
    session, stores, key, digest, admission = admit_one(tmp_path)
    poster = FakePoster(exc=TimeoutError("timeout"))
    assert session.post_once(poster).outcome == "POST_UNKNOWN"
    present = FakeReader(acknowledgment_marker(digest) + b"\nextra")
    report = session.read_back_once(present, key)
    assert report.confirmation == "PRESENT" and report.reader_calls == 1 and report.unknown is False
    inspected = open_existing(session.path)
    assert inspected.outbox[0][5] == "PRESENT" and inspected.outbox[0][3] == "POST_UNKNOWN"
    assert inspected.deliveries[0][6] == admission
    assert session.post_once(poster).poster_calls == 0 and len(poster.calls) == 1
    assert len(ledger_rows(stores)) == 1

    session, _stores, key, digest, _admission = admit_one(tmp_path / "miss")
    session.post_once(FakePoster())
    missing = session.read_back_once(FakeReader(b"unrelated"), key)
    assert missing.confirmation == "NOT_FOUND_AT_CHECK"
    assert open_existing(session.path).outbox[0][3] == "POSTED"
    assert session.post_once(FakePoster()).poster_calls == 0

    session, _stores, key, _digest, _admission = admit_one(tmp_path / "err")
    session.post_once(FakePoster())
    failed = session.read_back_once(FakeReader(exc=RuntimeError("read")), key)
    assert failed.confirmation == "NOT_CHECKED" and failed.unknown is True
    assert open_existing(session.path).outbox[0][5] == "NOT_CHECKED"


def test_read_back_commit_seams_stay_not_checked_for_the_caller(tmp_path, monkeypatch):
    real = sim.commit_readback
    session, _stores, key, digest, _admission = admit_one(tmp_path)
    session.post_once(FakePoster())
    monkeypatch.setattr(sim, "commit_readback", lambda _connection: (_ for _ in ()).throw(
        sqlite3.OperationalError("before")))
    report = session.read_back_once(FakeReader(acknowledgment_marker(digest)), key)
    assert report.confirmation == "NOT_CHECKED" and report.unknown is True
    assert open_existing(session.path).outbox[0][5] == "NOT_CHECKED"

    def after(connection):
        real(connection)
        raise sqlite3.OperationalError("after")

    session, _stores, key, digest, _admission = admit_one(tmp_path / "after")
    session.post_once(FakePoster())
    monkeypatch.setattr(sim, "commit_readback", after)
    report = session.read_back_once(FakeReader(acknowledgment_marker(digest)), key)
    assert report.confirmation == "NOT_CHECKED" and report.unknown is True
    assert open_existing(session.path).outbox[0][5] == "PRESENT"
    assert open_existing(session.path).outbox[0][3] == "POSTED"


def test_unconfirmed_ack_is_not_posted(tmp_path, monkeypatch):
    real = sim.commit_outbox

    def after(connection):
        real(connection)
        raise sqlite3.OperationalError("after")

    monkeypatch.setattr(sim, "commit_outbox", after)
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    body = envelope_frame(make_envelope(stores.signers))
    report = session.poll_once(ScriptSource({None: page((obs(1, body),))}), stores.adapter(), NOW, thread=23)
    assert "NOT_CONFIRMED" in open_existing(session.path).deliveries[0][6]
    poster = FakePoster()
    posted = session.post_once(poster)
    assert posted.poster_calls == 0 and poster.calls == []
    assert report.manual_required is True


# --- P7: second owner, commit_refusal, and concurrent intake -------------------

def _p7_child(path):
    """Fresh interpreter view of one fixture file. Must not gain a poll owner."""
    from pathlib import Path

    from skippy.collaboration.offline_bridge_transport_sim import (
        REPOSITORY,
        FixtureSession,
        TransportFixtureError,
        create_fresh,
        open_existing,
    )

    target = Path(path)
    created = True
    try:
        create_fresh(target, REPOSITORY, (23, 26))
    except TransportFixtureError:
        created = False
    view = open_existing(target)
    constructed = True
    try:
        FixtureSession(target, REPOSITORY, (23, 26))
    except TransportFixtureError:
        constructed = False
    delivery = view.deliveries[0] if view.deliveries else None
    posted = view.outbox[0] if view.outbox else None
    return {
        "created": created,
        "constructed": constructed,
        "blocking": view.blocking,
        "has_poll": hasattr(view, "poll_once"),
        "state": None if delivery is None else delivery[5],
        "digest": None if delivery is None else bytes(delivery[3]),
        "result": None if delivery is None else delivery[6],
        "post_state": None if posted is None else posted[3],
    }


def _p7_child_put(path, queue):
    try:
        queue.put(_p7_child(path))
    except Exception as exc:
        queue.put({"error": repr(exc)})


def _spawn(path):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_p7_child_put, args=(str(path), queue))
    proc.start()
    proc.join(60)
    assert proc.exitcode == 0
    payload = queue.get(timeout=10)
    assert "error" not in payload, payload
    return payload


def test_new_process_cannot_poll_prepared_or_attempting(tmp_path, monkeypatch):
    real_prepared = sim.commit_prepared

    def prepared_after(connection):
        real_prepared(connection)
        raise sqlite3.OperationalError("after")

    monkeypatch.setattr(sim, "commit_prepared", prepared_after)
    stores = AdmissionStores(tmp_path / "prep" / "adm")
    session = create_fresh(tmp_path / "prep" / "transport.db", REPOSITORY, (23, 26))
    body = envelope_frame(make_envelope(stores.signers))
    report = session.poll_once(ScriptSource({None: page((obs(1, body),))}), stores.adapter(), NOW, thread=23)
    assert report.deliver_calls == 0
    child = _spawn(session.path)
    assert child == {
        "created": False,
        "constructed": False,
        "blocking": True,
        "has_poll": False,
        "state": "PREPARED",
        "digest": body_digest(body),
        "result": None,
        "post_state": None,
    }

    monkeypatch.setattr(sim, "commit_prepared", real_prepared)
    real_attempt = sim.commit_post_attempt

    def attempt_after(connection):
        real_attempt(connection)
        raise sqlite3.OperationalError("after")

    monkeypatch.setattr(sim, "commit_post_attempt", attempt_after)
    (tmp_path / "post").mkdir()
    posted = create_fresh(tmp_path / "post" / "transport.db", REPOSITORY, (23, 26))
    post_stores = AdmissionStores(tmp_path / "post" / "adm")
    signed = envelope_frame(make_envelope(post_stores.signers))
    assert posted.poll_once(
        ScriptSource({None: page((obs(1, signed),))}), post_stores.adapter(), NOW, thread=23,
    ).dispositions[0].kind == "ADMITTED"
    attempt = posted.post_once(FakePoster())
    assert attempt.unknown is True and attempt.poster_calls == 0 and attempt.outcome is None
    child = _spawn(posted.path)
    assert child["created"] is False and child["constructed"] is False and child["has_poll"] is False
    assert child["post_state"] == "ATTEMPTING"
    assert "ADMITTED" in child["result"]


def test_concurrent_owner_threads_do_not_double_admit_or_duplicate_ties(tmp_path):
    import threading

    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    signed = envelope_frame(make_envelope(stores.signers))
    source = ScriptSource({None: page((obs(1, signed),))})
    adapter = CountingAdapter(stores.adapter())
    reports, errors = [], []

    def run():
        try:
            reports.append(session.poll_once(source, adapter, NOW, thread=23))
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    assert adapter.calls == 1
    assert sum(item.deliver_calls for item in reports) == 1
    assert intent_states(stores) == [("ACCEPTED",)]
    kinds = [item.kind for report in reports for item in report.dispositions]
    assert kinds.count("ADMITTED") == 1

    tied = create_fresh(tmp_path / "ties.db", REPOSITORY, (23, 26))
    pages = {
        None: page((obs(9, b"later-id", TS), obs(4, b"lower-id", TS)), token="p2"),
        "p2": page((obs(2, b"next", TS2),), token=None),
    }
    shared = ScriptSource(pages)
    again, failures = [], []

    def run_ties():
        try:
            again.append(tied.poll_once(shared, object(), NOW, thread=23))
        except Exception as exc:
            failures.append(exc)

    workers = [threading.Thread(target=run_ties) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert failures == []
    rows = open_existing(tied.path).deliveries
    assert [row[2] for row in rows] == [2, 4, 9]
    assert sum(item.deliver_calls for item in again) == 0
    assert len({row[2] for row in rows}) == 3


def test_refusal_commit_seams_stop_without_a_deliver(tmp_path, monkeypatch):
    real = sim.commit_refusal
    monkeypatch.setattr(
        sim, "commit_refusal", lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("before")),
    )
    session = create_fresh(tmp_path / "before.db", REPOSITORY, (23, 26))
    report = session.poll_once(
        ScriptSource({None: page((obs(1, b"one"), obs(2, b"two", TS2)))}), object(), NOW, thread=23,
    )
    assert report.deliver_calls == 0 and report.manual_required is True
    assert open_existing(session.path).deliveries == ()

    def after(connection):
        real(connection)
        raise sqlite3.OperationalError("after")

    monkeypatch.setattr(sim, "commit_refusal", after)
    later = create_fresh(tmp_path / "after.db", REPOSITORY, (23, 26))
    report = later.poll_once(
        ScriptSource({None: page((obs(1, b"one"), obs(2, b"two", TS2)))}), object(), NOW, thread=23,
    )
    assert report.deliver_calls == 0 and report.manual_required is True
    rows = open_existing(later.path).deliveries
    assert len(rows) == 1 and rows[0][2] == 1 and rows[0][5] == "FINAL"
    assert later.poll_once(ScriptSource({None: page(())}), object(), NOW, thread=23).source_calls == 0


def test_edit_pin_is_what_a_new_process_can_see(tmp_path, monkeypatch):
    stores = AdmissionStores(tmp_path / "adm")
    session = create_fresh(tmp_path / "transport.db", REPOSITORY, (23, 26))
    original = envelope_frame(make_envelope(stores.signers))
    adapter = CountingAdapter(stores.adapter())
    assert session.poll_once(
        ScriptSource({None: page((obs(1, original),))}), adapter, NOW, thread=23,
    ).dispositions[0].kind == "ADMITTED"
    monkeypatch.setattr(sim, "deliver_frame", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("deliver")))
    report = session.poll_once(
        ScriptSource({None: page((obs(1, fence(b'{ "a" : 1 }'), TS2),))}), adapter, NOW, thread=23,
    )
    assert report.dispositions[0].kind == "EDIT_HELD"
    assert report.deliver_calls == 0 and adapter.calls == 1
    child = _spawn(session.path)
    assert child["created"] is False and child["constructed"] is False and child["has_poll"] is False
    assert child["digest"] == body_digest(original)
    assert "ADMITTED" in child["result"]
    assert child["blocking"] is False
