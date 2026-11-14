"""BRIDGE-02B.2.2 offline transport fixture tests.

OFFLINE SYNTHETIC ONLY. Governing contract: J13.6. P3 covers the bounded fake
source, exact frame extraction and delivery identity. P4 covers the disposable
journal, outbox and continuation. The poster is a later priority.
execution_enabled=False. No network or credentials.
"""

from __future__ import annotations

import ast
import hashlib
import json
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
        "__future__", "hashlib", "re", "dataclasses", "json", "os", "sqlite3", "pathlib", "skippy",
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
