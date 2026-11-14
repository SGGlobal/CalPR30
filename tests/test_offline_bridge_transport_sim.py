"""BRIDGE-02B.2.2 offline transport fixture tests.

OFFLINE SYNTHETIC ONLY. Governing contract: J13.6. P3 covers the bounded fake
source, exact frame extraction and delivery identity. Later priorities add the
journal, outbox and poster. execution_enabled=False. No network or credentials.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

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
    extract_frame,
    frame_digest,
    normalize_observations,
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
    assert imported <= {"__future__", "hashlib", "re", "dataclasses"}
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
