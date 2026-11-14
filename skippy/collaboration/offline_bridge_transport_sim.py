"""BRIDGE-02B.2.2 offline transport fixture (J13.6).

OFFLINE SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

Work order https://github.com/SGGlobal/QLA/issues/23#issuecomment-6873540007.
The governing contract is J13.6 of docs/bridge-02b2-compatibility-contract.md.

This module is a disposable fixture. It does not open a remote connection, read
secrets, post to a carrier, or schedule work. Signed admission stays with the
unchanged ingress ``deliver`` path. ``execution_enabled`` remains False.

P3: bounded fake pages, exact frame extraction, and delivery identity.
P4: one disposable transport database, PREPARED before ``deliver``, a local
outbox sink, FINAL, and a continuation hint written only after confirmed
finals. P5 adds a fake poster and a one-shot read-back. Nothing here is a live
transport or a claim of source completeness, freshness, or exactly-once delivery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from skippy.collaboration.offline_bridge_ingress import deliver as deliver_frame

execution_enabled = False

REPOSITORY = "SGGlobal/QLA"
DEFAULT_THREADS = (23, 26)
MAX_PAGES = 4
MAX_PER_PAGE = 25
MAX_OBSERVATIONS = 100
MAX_BODY_BYTES = 131072
MAX_AUTHOR_BYTES = 256
MAX_TOKEN_BYTES = 128
MIN_FRAME_BYTES = 1
MAX_FRAME_BYTES = 65536
OPEN_FENCE = b"```qla-bridge-frame-v1"
CLOSE_FENCE = b"```"
_BOM = b"\xef\xbb\xbf"
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class TransportFixtureError(Exception):
    """Fail-closed fixture error. Not an admission result."""


class SourceRefusal(TransportFixtureError):
    """The fake page broke the fixture profile. Callers must not deliver."""


class ContradictoryObservations(SourceRefusal):
    """One comment_id was observed with two different bodies. Hold the batch."""

    def __init__(self, comment_id: int):
        super().__init__("contradictory observations")
        self.comment_id = comment_id


class _RestartListing(Exception):
    """Resume cursor does not match this snapshot. Start again at the first page."""


@dataclass(frozen=True)
class CommentObservation:
    """One fake comment. ``author`` is audit text and is not an actor."""

    comment_id: int
    updated_at: str
    author: str
    body: bytes


@dataclass(frozen=True)
class CommentPage:
    """One immutable fake page. ``next_token`` is a traversal hint, not a GitHub cursor."""

    snapshot_id: str
    observations: tuple[CommentObservation, ...]
    next_token: str | None


@dataclass(frozen=True)
class ExtractedFrame:
    """Byte slice between the two framing LF separators. Not re-encoded."""

    frame: bytes
    start: int
    end: int


@dataclass(frozen=True)
class FrameRefusal:
    """Transport refusal before ``deliver``. ``reason`` is fixture text, not an admission."""

    reason: str


@dataclass(frozen=True)
class CollectedBatch:
    """Normalized observations from at most four fake pages."""

    observations: tuple[CommentObservation, ...]
    snapshot_id: str
    next_token: str | None
    complete: bool
    source_calls: int
    restarted: bool


def body_digest(body: bytes) -> bytes:
    """SHA-256 of the entire observed comment body, including a refused frame."""
    if type(body) is not bytes:
        raise TransportFixtureError("body must be bytes")
    return hashlib.sha256(body).digest()


def frame_digest(frame: bytes) -> bytes:
    """SHA-256 of an extracted frame slice."""
    if type(frame) is not bytes:
        raise TransportFixtureError("frame must be bytes")
    return hashlib.sha256(frame).digest()


def classify_delivery(prior_digest: bytes | None, body: bytes) -> str:
    """Delivery identity against a pinned body digest.

    Returns ``new``, ``duplicate`` or ``edit``. An edit is a changed body under
    the same comment identity, including whitespace that would not change the
    canonical envelope. The caller holds an edit before ``deliver``.
    """
    digest = body_digest(body)
    if prior_digest is None:
        return "new"
    if type(prior_digest) is not bytes or len(prior_digest) != 32:
        raise TransportFixtureError("prior digest must be 32 bytes")
    if prior_digest == digest:
        return "duplicate"
    return "edit"


def _ascii_bounded(value: str, *, what: str) -> None:
    if type(value) is not str or value == "":
        raise SourceRefusal(what)
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SourceRefusal(what) from exc
    if len(raw) > MAX_TOKEN_BYTES:
        raise SourceRefusal(what)


def _valid_timestamp(value: str) -> bool:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        return False
    # Round-trip rejects impossible calendar values without normalizing the text.
    year, month, day = int(value[0:4]), int(value[5:7]), int(value[8:10])
    hour, minute, second = int(value[11:13]), int(value[14:16]), int(value[17:19])
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 59):
        return False
    # February and short months: reject dates datetime would roll over.
    month_lengths = (0, 31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return day <= month_lengths[month]


def validate_observation(observation: CommentObservation) -> None:
    """Reject a single observation that breaks the fixture profile."""
    if type(observation) is not CommentObservation:
        raise SourceRefusal("observation type")
    comment_id = observation.comment_id
    if type(comment_id) is not int or comment_id <= 0:
        raise SourceRefusal("comment_id")
    if not _valid_timestamp(observation.updated_at):
        raise SourceRefusal("updated_at")
    author = observation.author
    if type(author) is not str:
        raise SourceRefusal("author")
    try:
        author_bytes = author.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceRefusal("author") from exc
    if len(author_bytes) > MAX_AUTHOR_BYTES:
        raise SourceRefusal("author")
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in author):
        raise SourceRefusal("author")
    body = observation.body
    if type(body) is not bytes:
        raise SourceRefusal("body")
    if len(body) > MAX_BODY_BYTES:
        raise SourceRefusal("body")


def validate_page(page: CommentPage) -> None:
    """Reject one page that breaks the fixture profile. Does not admit anything."""
    if type(page) is not CommentPage:
        raise SourceRefusal("page type")
    _ascii_bounded(page.snapshot_id, what="snapshot_id")
    if page.next_token is not None:
        _ascii_bounded(page.next_token, what="token")
    observations = page.observations
    if type(observations) is not tuple:
        raise SourceRefusal("observations")
    if len(observations) > MAX_PER_PAGE:
        raise SourceRefusal("page size")
    for observation in observations:
        validate_observation(observation)


def normalize_observations(
    observations: tuple[CommentObservation, ...],
) -> tuple[CommentObservation, ...]:
    """Collapse exact and same-body duplicates, and hold contradictory bodies.

    Same ``comment_id`` and identical body bytes are one observation, even when
    ``updated_at`` or ``author`` differ. Two different bodies for one
    ``comment_id`` raise ``ContradictoryObservations`` so the caller can hold
    the whole batch before ``deliver``. Remaining observations are ordered by
    ``(updated_at, comment_id)``. That order is not a signed sequence.
    """
    if type(observations) is not tuple:
        raise SourceRefusal("observations")
    if len(observations) > MAX_OBSERVATIONS:
        raise SourceRefusal("observation total")
    grouped: dict[int, list[CommentObservation]] = {}
    for observation in observations:
        validate_observation(observation)
        grouped.setdefault(observation.comment_id, []).append(observation)
    chosen: list[CommentObservation] = []
    for comment_id, items in grouped.items():
        bodies = {item.body for item in items}
        if len(bodies) != 1:
            raise ContradictoryObservations(comment_id)
        chosen.append(min(items, key=lambda item: (item.updated_at, item.author)))
    chosen.sort(key=lambda item: (item.updated_at, item.comment_id))
    return tuple(chosen)


def _line_table(body: bytes) -> tuple[list[bytes], list[int]]:
    parts = body.split(b"\n")
    offsets: list[int] = []
    pos = 0
    for index, part in enumerate(parts):
        offsets.append(pos)
        pos += len(part)
        if index != len(parts) - 1:
            pos += 1
    return parts, offsets


def _fence_candidate(line: bytes) -> bool:
    """A line that is a fence, an indented fence, or a tag variant."""
    stripped = line.lstrip(b" \t")
    return stripped.startswith(b"```")


def extract_frame(body: bytes) -> ExtractedFrame | FrameRefusal:
    """Extract the single ``qla-bridge-frame-v1`` slice without altering its bytes.

    The profile is one opening line whose bytes are exactly three backticks plus
    ``qla-bridge-frame-v1``, a LF, the frame bytes, a LF, and a closing line whose
    bytes are exactly three backticks. The closing line may end the body or be
    followed by a LF and commentary. CRLF, indented fences, tag variants, zero
    blocks, extra fence lines and an empty or oversize slice are refusals.
    The returned slice is the original body range between those two LF separators.
    """
    if type(body) is not bytes:
        return FrameRefusal("not-bytes")
    if body.startswith(_BOM):
        return FrameRefusal("bom")
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return FrameRefusal("utf8")
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return FrameRefusal("utf8")
    lines, offsets = _line_table(body)
    open_at = [index for index, line in enumerate(lines) if line == OPEN_FENCE]
    close_at = [index for index, line in enumerate(lines) if line == CLOSE_FENCE]
    if len(open_at) == 0:
        return FrameRefusal("no-frame")
    if len(open_at) != 1 or len(close_at) != 1 or close_at[0] <= open_at[0]:
        return FrameRefusal("fence")
    for index, line in enumerate(lines):
        if index in (open_at[0], close_at[0]):
            continue
        if _fence_candidate(line):
            return FrameRefusal("fence")
    start = offsets[open_at[0]] + len(lines[open_at[0]]) + 1
    end = offsets[close_at[0]] - 1
    if end < start:
        return FrameRefusal("empty-frame")
    frame = body[start:end]
    if len(frame) < MIN_FRAME_BYTES:
        return FrameRefusal("empty-frame")
    if len(frame) > MAX_FRAME_BYTES:
        return FrameRefusal("frame-too-large")
    if body[end:end + 1] != b"\n":
        return FrameRefusal("fence")
    return ExtractedFrame(frame, start, end)


def _fetch_page(source, repository: str, thread: int, token: str | None, counter: list[int]):
    try:
        page = source.page(repository, thread, token)
    except Exception:
        counter[0] += 1
        raise
    counter[0] += 1
    return page


def _collect_once(
    source,
    repository: str,
    thread: int,
    start_token: str | None,
    stored_snapshot: str | None,
    counter: list[int],
    *,
    allow_restart: bool,
) -> CollectedBatch:
    if type(repository) is not str or repository != REPOSITORY:
        raise SourceRefusal("repository")
    if type(thread) is not int or thread <= 0:
        raise SourceRefusal("thread")
    if start_token is not None:
        _ascii_bounded(start_token, what="token")
    if stored_snapshot is not None:
        _ascii_bounded(stored_snapshot, what="snapshot_id")
    seen: set[str | None] = set()
    token: str | None = start_token
    snapshot: str | None = None
    pages: list[CommentPage] = []
    for _ in range(MAX_PAGES):
        if token in seen:
            raise SourceRefusal("repeated token")
        seen.add(token)
        try:
            page = _fetch_page(source, repository, thread, token, counter)
        except Exception as exc:
            if allow_restart and not pages:
                raise _RestartListing() from exc
            raise
        try:
            validate_page(page)
        except SourceRefusal:
            raise
        if snapshot is None:
            snapshot = page.snapshot_id
            if stored_snapshot is not None and snapshot != stored_snapshot:
                if allow_restart and len(pages) == 0:
                    raise _RestartListing()
                raise SourceRefusal("snapshot")
        elif page.snapshot_id != snapshot:
            raise SourceRefusal("snapshot changed")
        pages.append(page)
        if page.next_token is None:
            observations = tuple(item for page in pages for item in page.observations)
            return CollectedBatch(
                observations=normalize_observations(observations),
                snapshot_id=snapshot,
                next_token=None,
                complete=True,
                source_calls=counter[0],
                restarted=False,
            )
        if page.next_token in seen:
            raise SourceRefusal("repeated token")
        token = page.next_token
    observations = tuple(item for page in pages for item in page.observations)
    return CollectedBatch(
        observations=normalize_observations(observations),
        snapshot_id=snapshot if snapshot is not None else "",
        next_token=token,
        complete=False,
        source_calls=counter[0],
        restarted=False,
    )


def collect_observations(
    source,
    repository: str,
    thread: int,
    *,
    start_token: str | None = None,
    stored_snapshot: str | None = None,
) -> CollectedBatch:
    """Read at most four fake pages and return one normalized batch.

    A non-null ``next_token`` after four pages means the fixture stopped at its
    bound. That is an incomplete hint, not proof the source is exhausted.
    A resumed token whose snapshot differs, or whose ``page`` call raises, is
    read again from the first page once. Structural refusals (types, sizes,
    repeated tokens, a snapshot that changes mid-pass, contradictory bodies)
    propagate and must not be admitted. A source exception on a fresh listing
    propagates so the caller can report manual failure instead of an empty success.
    """
    counter = [0]
    if start_token is not None:
        try:
            return _collect_once(
                source, repository, thread, start_token, stored_snapshot, counter,
                allow_restart=True,
            )
        except _RestartListing:
            batch = _collect_once(
                source, repository, thread, None, None, counter, allow_restart=False,
            )
            return CollectedBatch(
                observations=batch.observations,
                snapshot_id=batch.snapshot_id,
                next_token=batch.next_token,
                complete=batch.complete,
                source_calls=counter[0],
                restarted=True,
            )
    return _collect_once(
        source, repository, thread, None, None, counter, allow_restart=False,
    )


# --- disposable journal, outbox and continuation (P4) -------------------------

FIXTURE_VERSION = 1
_SCHEMA = {
    "fixture_meta": ("version", "repository", "allowed_threads"),
    "deliveries": (
        "repository", "thread", "comment_id", "body_digest", "frame_digest", "state", "result_json",
    ),
    "outbox": (
        "delivery_key", "ack_json", "ack_digest", "post_state", "external_id", "confirmation",
    ),
    "continuation": ("thread", "snapshot_id", "token"),
}
_SAFE_NULL_POISON = {
    ("ADMITTED", "CONFIRMED_BY_SINK"),
    ("MALFORMED", "NOT_ATTEMPTED"),
}


class InspectionError(TransportFixtureError):
    """Existing fixture file cannot be inspected. Nothing was repaired."""


@dataclass(frozen=True)
class Disposition:
    """One observation's transport outcome. ``author`` is audit text only."""

    thread: int
    comment_id: int
    kind: str
    adapter_calls: int
    author: str
    reason: str | None = None


@dataclass(frozen=True)
class PollReport:
    """Intake result. ``manual_required`` means this pass must not continue itself."""

    dispositions: tuple[Disposition, ...]
    complete: bool
    continuation_token: str | None
    snapshot_id: str | None
    manual_required: bool
    deliver_calls: int
    source_calls: int
    status: str


@dataclass(frozen=True)
class PostOutcome:
    """Fake poster result. ``POSTED`` carries a positive integer id, not a bool."""

    outcome: str
    external_id: int | None = None


@dataclass(frozen=True)
class PostReport:
    """One posting attempt. ``unknown`` is not one of the four posting outcomes."""

    outcome: str | None
    unknown: bool
    manual_required: bool
    poster_calls: int
    delivery_key: str | None
    external_id: int | None


@dataclass(frozen=True)
class ReadReport:
    """One fake read. An unknown write leaves confirmation ``NOT_CHECKED``."""

    confirmation: str
    manual_required: bool
    unknown: bool
    reader_calls: int


@dataclass(frozen=True)
class Inspection:
    """Read-only view. Has no poll, post, or resume operation."""

    version: int
    repository: str
    allowed_threads: tuple[int, ...]
    deliveries: tuple[tuple, ...]
    outbox: tuple[tuple, ...]
    continuation: tuple[tuple, ...]
    blocking: bool


def commit_prepared(connection) -> None:
    """Transaction A. Tests may wrap this seam; a whole-poll wrapper is not a substitute."""
    connection.commit()


def commit_outbox(connection) -> None:
    """Transaction B, inside the ingress sink, before control returns to the poll."""
    connection.commit()


def commit_final(connection) -> None:
    """Transaction C. Records the IngressResult returned by ``deliver``."""
    connection.commit()


def commit_refusal(connection) -> None:
    """Direct FINAL for a frame the fixture refused before ``deliver``."""
    connection.commit()


def commit_continuation(connection) -> None:
    """Transaction D. Runs only after every processed observation has a confirmed FINAL."""
    connection.commit()


def commit_post_attempt(connection) -> None:
    """Commits post_state=ATTEMPTING before the fake poster is called."""
    connection.commit()


def commit_post_result(connection) -> None:
    """Commits the observed post outcome. A failure leaves the attempt unknown."""
    connection.commit()


def commit_readback(connection) -> None:
    """Commits PRESENT or NOT_FOUND_AT_CHECK. A failure stays NOT_CHECKED to the caller."""
    connection.commit()


def acknowledgment_marker(digest: bytes) -> bytes:
    """Exact bytes a fake read must contain. Not a live GitHub marker."""
    if type(digest) is not bytes or len(digest) != 32:
        raise TransportFixtureError("digest")
    return b"qla-bridge-ack-v1\n" + digest.hex().encode("ascii") + b"\n"


def _interpret_post(returned) -> tuple[str, int | None]:
    """Map a poster return to one of the three outcomes. Anything else is unknown."""
    if type(returned) is not PostOutcome:
        return "POST_UNKNOWN", None
    if returned.outcome == "POSTED" and type(returned.external_id) is int and returned.external_id > 0:
        return "POSTED", returned.external_id
    if returned.outcome == "NOT_POSTED":
        return "NOT_POSTED", None
    if returned.outcome == "POST_UNKNOWN":
        return "POST_UNKNOWN", None
    return "POST_UNKNOWN", None


def delivery_key(repository: str, thread: int, comment_id: int) -> str:
    return f"{repository}\x1f{thread}\x1f{comment_id}"


def _require_disabled() -> None:
    if execution_enabled is not False:
        raise TransportFixtureError("execution is disabled")


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _rollback(connection) -> None:
    try:
        connection.rollback()
    except Exception:
        return


def _dump(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ack_bytes(record) -> bytes:
    correlation = None
    if record.correlation is not None:
        correlation = {
            "message_id": record.correlation.message_id,
            "idempotency_key": record.correlation.idempotency_key,
            "sequence": record.correlation.sequence,
        }
    return _dump({
        "record_version": record.record_version,
        "admission": record.admission.value,
        "adapter_step": record.adapter_step,
        "correlation": correlation,
        "correlation_verified": bool(record.correlation_verified),
    }).encode("utf-8")


def _ingress_payload(result) -> dict:
    correlation = None
    if result.correlation is not None:
        correlation = {
            "message_id": result.correlation.message_id,
            "idempotency_key": result.correlation.idempotency_key,
            "sequence": result.correlation.sequence,
        }
    return {
        "admission": result.admission.value,
        "adapter_step": result.adapter_step,
        "adapter_classification": result.adapter_classification,
        "poisoned": result.poisoned,
        "adapter_calls": result.adapter_calls,
        "correlation": correlation,
        "correlation_verified": bool(result.correlation_verified),
        "ack": result.ack.value,
        "detail": result.detail,
    }


def _blocks_payload(payload: dict) -> bool:
    """True when a FINAL row must stop later automatic intake."""
    if "transport_disposition" in payload and "admission" not in payload:
        return False
    admission = payload.get("admission")
    ack = payload.get("ack")
    poisoned = payload.get("poisoned", None)
    if admission in ("UNCERTAIN", "STOP_RETAINED") or ack == "NOT_CONFIRMED" or poisoned is True:
        return True
    if poisoned is None and (admission, ack) in _SAFE_NULL_POISON:
        return False
    if poisoned is False and ack == "CONFIRMED_BY_SINK" and admission in ("ADMITTED", "REFUSED", "REJECTED"):
        return False
    return True


def _result_blocks(result) -> bool:
    return _blocks_payload(_ingress_payload(result))


class _OutboxSink:
    """J12 recording sink. One local ack row. No posting."""

    def __init__(self, path: Path, key: str):
        self.path = path
        self.key = key
        self.calls = 0

    def record(self, record) -> None:
        self.calls += 1
        if self.calls != 1:
            raise TransportFixtureError("sink already recorded")
        raw = _ack_bytes(record)
        connection = _connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO outbox(delivery_key, ack_json, ack_digest, post_state, external_id, confirmation) "
                "VALUES (?, ?, ?, 'NOT_ATTEMPTED', NULL, 'NOT_CHECKED')",
                (self.key, raw.decode("utf-8"), hashlib.sha256(raw).digest()),
            )
            commit_outbox(connection)
        finally:
            connection.close()


class _BoundSource:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def page(self, repository, thread, token):
        self.calls += 1
        return self.inner.page(repository, thread, token)


def _columns(connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))


def _validate_connection(connection) -> sqlite3.Row:
    names = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if names != set(_SCHEMA):
        raise InspectionError("schema")
    for table, columns in _SCHEMA.items():
        if _columns(connection, table) != columns:
            raise InspectionError(table)
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    sync = connection.execute("PRAGMA synchronous").fetchone()[0]
    if str(mode).lower() != "wal" or sync != 2:
        raise InspectionError("durability")
    rows = connection.execute(
        "SELECT version, repository, allowed_threads FROM fixture_meta"
    ).fetchall()
    if len(rows) != 1 or rows[0]["version"] != FIXTURE_VERSION:
        raise InspectionError("meta")
    states = {row[0] for row in connection.execute("SELECT DISTINCT state FROM deliveries")}
    if not states <= {"PREPARED", "FINAL"}:
        raise InspectionError("state")
    return rows[0]


def _row_blocks(state: str, result_json: str | None) -> bool:
    if state == "PREPARED":
        return True
    if state != "FINAL":
        return True
    if result_json is None:
        return True
    try:
        payload = json.loads(result_json)
    except json.JSONDecodeError:
        return True
    if type(payload) is not dict:
        return True
    return _blocks_payload(payload)


def _store_blocks(connection) -> bool:
    rows = connection.execute("SELECT state, result_json FROM deliveries").fetchall()
    return any(_row_blocks(row["state"], row["result_json"]) for row in rows)


def create_fresh(path, repository: str, threads: tuple[int, ...]) -> "FixtureSession":
    """Create one disposable transport file. An existing path is refused."""
    _require_disabled()
    if type(repository) is not str or repository != REPOSITORY:
        raise TransportFixtureError("repository")
    if type(threads) is not tuple or not threads:
        raise TransportFixtureError("threads")
    cleaned: list[int] = []
    for thread in threads:
        if type(thread) is not int or thread <= 0 or thread in cleaned:
            raise TransportFixtureError("thread")
        cleaned.append(thread)
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise TransportFixtureError("path exists")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(target) + suffix).exists():
            raise TransportFixtureError("sidecar")
    if not target.parent.is_dir():
        raise TransportFixtureError("parent")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    connection = _connect(target)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in (
            "CREATE TABLE fixture_meta (version INTEGER NOT NULL, repository TEXT NOT NULL, "
            "allowed_threads TEXT NOT NULL)",
            "CREATE TABLE deliveries (repository TEXT NOT NULL, thread INTEGER NOT NULL, "
            "comment_id INTEGER NOT NULL, body_digest BLOB NOT NULL, frame_digest BLOB, "
            "state TEXT NOT NULL, result_json TEXT, PRIMARY KEY (repository, thread, comment_id))",
            "CREATE TABLE outbox (delivery_key TEXT NOT NULL UNIQUE, ack_json TEXT NOT NULL, "
            "ack_digest BLOB NOT NULL, post_state TEXT NOT NULL, external_id INTEGER, "
            "confirmation TEXT NOT NULL)",
            "CREATE TABLE continuation (thread INTEGER NOT NULL PRIMARY KEY, snapshot_id TEXT NOT NULL, "
            "token TEXT)",
        ):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO fixture_meta(version, repository, allowed_threads) VALUES (?, ?, ?)",
            (FIXTURE_VERSION, repository, json.dumps(cleaned)),
        )
        connection.commit()
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()
    return FixtureSession(target, repository, tuple(cleaned), _owner=_SESSION_OWNER)


def open_existing(path) -> Inspection:
    """Validate a fixture file and return rows. Does not poll, post, or repair."""
    _require_disabled()
    target = Path(path)
    if not target.is_file():
        raise InspectionError("missing")
    connection = sqlite3.connect(
        target.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        meta = _validate_connection(connection)
        try:
            threads = tuple(json.loads(meta["allowed_threads"]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise InspectionError("threads") from exc
        if type(threads) is not tuple or not all(type(item) is int and item > 0 for item in threads):
            raise InspectionError("threads")
        deliveries = tuple(
            tuple(row) for row in connection.execute(
                "SELECT repository, thread, comment_id, body_digest, frame_digest, state, result_json "
                "FROM deliveries ORDER BY thread, comment_id"
            )
        )
        outbox = tuple(
            tuple(row) for row in connection.execute(
                "SELECT delivery_key, ack_json, ack_digest, post_state, external_id, confirmation "
                "FROM outbox ORDER BY delivery_key"
            )
        )
        continuation = tuple(
            tuple(row) for row in connection.execute(
                "SELECT thread, snapshot_id, token FROM continuation ORDER BY thread"
            )
        )
        blocking = _store_blocks(connection)
    finally:
        connection.close()
    return Inspection(
        version=FIXTURE_VERSION, repository=meta["repository"], allowed_threads=threads,
        deliveries=deliveries, outbox=outbox, continuation=continuation, blocking=blocking,
    )


_SESSION_OWNER = object()


def _serialized(method):
    """One session handles one poll, post or read at a time. Not a cross-process lock."""

    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class FixtureSession:
    """One owner of one fresh transport file. Reopening is ``open_existing`` only."""

    def __init__(self, path: Path, repository: str, threads: tuple[int, ...], *, _owner=None):
        if _owner is not _SESSION_OWNER:
            raise TransportFixtureError("use create_fresh")
        self.path = path
        self.repository = repository
        self.threads = threads
        self.manual_required = False
        self._lock = threading.Lock()

    def _blocked(self) -> bool:
        if self.manual_required:
            return True
        connection = _connect(self.path)
        try:
            return _store_blocks(connection)
        finally:
            connection.close()

    def _prior(self, thread: int, comment_id: int):
        connection = _connect(self.path)
        try:
            return connection.execute(
                "SELECT body_digest, state FROM deliveries "
                "WHERE repository = ? AND thread = ? AND comment_id = ?",
                (self.repository, thread, comment_id),
            ).fetchone()
        finally:
            connection.close()

    def _write(self, sql: str, params: tuple, committer) -> bool:
        connection = _connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(sql, params)
            if cursor.rowcount != 1:
                raise TransportFixtureError("rowcount")
            committer(connection)
            return True
        except Exception:
            _rollback(connection)
            return False
        finally:
            connection.close()

    @_serialized
    def poll_once(self, source, adapter, now, *, thread: int) -> PollReport:
        """One bounded intake pass. Does not post and does not retry inside the pass."""
        _require_disabled()
        if self._blocked():
            return self._manual(source_calls=0, deliver_calls=0)
        if type(thread) is not int or thread not in self.threads:
            self.manual_required = True
            return self._manual(source_calls=0, deliver_calls=0, status="SOURCE_REFUSED")
        connection = _connect(self.path)
        try:
            saved = connection.execute(
                "SELECT snapshot_id, token FROM continuation WHERE thread = ?", (thread,)
            ).fetchone()
        finally:
            connection.close()
        start_token = None
        stored_snapshot = None
        if saved is not None and saved["token"] is not None:
            start_token = saved["token"]
            stored_snapshot = saved["snapshot_id"]
        bound = _BoundSource(source)
        try:
            batch = collect_observations(
                bound, self.repository, thread, start_token=start_token,
                stored_snapshot=stored_snapshot,
            )
        except ContradictoryObservations as exc:
            self.manual_required = True
            return PollReport(
                dispositions=(Disposition(thread, exc.comment_id, "EDIT_HELD", 0, "", "contradictory"),),
                complete=False, continuation_token=None, snapshot_id=None, manual_required=True,
                deliver_calls=0, source_calls=bound.calls, status="MANUAL",
            )
        except SourceRefusal:
            self.manual_required = True
            return PollReport(
                dispositions=(), complete=False, continuation_token=None, snapshot_id=None,
                manual_required=True, deliver_calls=0, source_calls=bound.calls, status="SOURCE_REFUSED",
            )
        except Exception:
            self.manual_required = True
            return PollReport(
                dispositions=(), complete=False, continuation_token=None, snapshot_id=None,
                manual_required=True, deliver_calls=0, source_calls=bound.calls, status="MANUAL",
            )
        dispositions: list[Disposition] = []
        deliver_calls = 0
        for observation in batch.observations:
            outcome = self._one(observation, thread, adapter, now)
            deliver_calls += outcome[0]
            if outcome[1] is not None:
                dispositions.append(outcome[1])
            if self.manual_required:
                break
        report_complete = False
        token = None
        if not self.manual_required:
            wrote = self._write(
                "INSERT INTO continuation(thread, snapshot_id, token) VALUES (?, ?, ?) "
                "ON CONFLICT(thread) DO UPDATE SET snapshot_id = excluded.snapshot_id, "
                "token = excluded.token",
                (thread, batch.snapshot_id, batch.next_token),
                commit_continuation,
            )
            if not wrote:
                self.manual_required = True
            else:
                report_complete = batch.complete
                token = batch.next_token
        status = "MANUAL" if self.manual_required else ("COMPLETE" if report_complete else "INCOMPLETE")
        return PollReport(
            dispositions=tuple(dispositions), complete=report_complete and not self.manual_required,
            continuation_token=token, snapshot_id=batch.snapshot_id, manual_required=self.manual_required,
            deliver_calls=deliver_calls, source_calls=bound.calls, status=status,
        )

    def _manual(self, *, source_calls: int, deliver_calls: int, status: str = "MANUAL") -> PollReport:
        return PollReport(
            dispositions=(), complete=False, continuation_token=None, snapshot_id=None,
            manual_required=True, deliver_calls=deliver_calls, source_calls=source_calls, status=status,
        )

    def _one(self, observation: CommentObservation, thread: int, adapter, now) -> tuple[int, Disposition | None]:
        prior = self._prior(thread, observation.comment_id)
        digest = body_digest(observation.body)
        if prior is not None and prior["state"] == "PREPARED":
            self.manual_required = True
            return 0, None
        if prior is not None and prior["state"] == "FINAL":
            decision = classify_delivery(bytes(prior["body_digest"]), observation.body)
            if decision == "duplicate":
                return 0, Disposition(
                    thread, observation.comment_id, "DUPLICATE", 0, observation.author, None,
                )
            self.manual_required = True
            return 0, Disposition(
                thread, observation.comment_id, "EDIT_HELD", 0, observation.author, "changed-body",
            )
        extracted = extract_frame(observation.body)
        if isinstance(extracted, FrameRefusal):
            payload = _dump({
                "transport_disposition": "FRAME_REFUSED",
                "reason": extracted.reason,
                "author": observation.author,
            })
            wrote = self._write(
                "INSERT INTO deliveries(repository, thread, comment_id, body_digest, frame_digest, "
                "state, result_json) VALUES (?, ?, ?, ?, NULL, 'FINAL', ?)",
                (self.repository, thread, observation.comment_id, digest, payload),
                commit_refusal,
            )
            if not wrote:
                self.manual_required = True
                return 0, None
            return 0, Disposition(
                thread, observation.comment_id, "TRANSPORT_REFUSED", 0, observation.author, extracted.reason,
            )
        key = delivery_key(self.repository, thread, observation.comment_id)
        prepared = self._write(
            "INSERT INTO deliveries(repository, thread, comment_id, body_digest, frame_digest, "
            "state, result_json) VALUES (?, ?, ?, ?, ?, 'PREPARED', NULL)",
            (self.repository, thread, observation.comment_id, digest, frame_digest(extracted.frame)),
            commit_prepared,
        )
        if not prepared:
            self.manual_required = True
            return 0, None
        sink = _OutboxSink(self.path, key)
        try:
            result = deliver_frame(extracted.frame, adapter, sink, now)
        except Exception:
            self.manual_required = True
            return 1, None
        except BaseException:
            self.manual_required = True
            raise
        payload = _dump(_ingress_payload(result))
        wrote = self._write(
            "UPDATE deliveries SET state = 'FINAL', result_json = ? "
            "WHERE repository = ? AND thread = ? AND comment_id = ? AND state = 'PREPARED'",
            (payload, self.repository, thread, observation.comment_id),
            commit_final,
        )
        if not wrote:
            self.manual_required = True
            return 1, None
        kind = result.admission.value
        if _result_blocks(result):
            self.manual_required = True
        return 1, Disposition(
            thread, observation.comment_id, kind, result.adapter_calls, observation.author, result.ack.value,
        )

    @_serialized
    def post_once(self, poster) -> PostReport:
        """Post at most one confirmed local ack. Never calls ``deliver`` and never reposts."""
        _require_disabled()
        if self.manual_required or self._post_blocked():
            self.manual_required = True
            return PostReport(None, True, True, 0, None, None)
        chosen = self._eligible_post()
        if chosen is None:
            return PostReport("NOT_ATTEMPTED", False, False, 0, None, None)
        key, digest, repository, thread = chosen
        wrote = self._write(
            "UPDATE outbox SET post_state = 'ATTEMPTING' "
            "WHERE delivery_key = ? AND post_state = 'NOT_ATTEMPTED'",
            (key,),
            commit_post_attempt,
        )
        if not wrote:
            self.manual_required = True
            return PostReport(None, True, True, 0, key, None)
        body = acknowledgment_marker(digest)
        try:
            returned = poster.post(repository, thread, body)
        except Exception:
            outcome, external_id = "POST_UNKNOWN", None
        except BaseException:
            self.manual_required = True
            raise
        else:
            outcome, external_id = _interpret_post(returned)
        wrote = self._write(
            "UPDATE outbox SET post_state = ?, external_id = ? "
            "WHERE delivery_key = ? AND post_state = 'ATTEMPTING'",
            (outcome, external_id, key),
            commit_post_result,
        )
        if not wrote:
            self.manual_required = True
            return PostReport(None, True, True, 1, key, None)
        return PostReport(outcome, False, False, 1, key, external_id)

    @_serialized
    def read_back_once(self, reader, key: str) -> ReadReport:
        """One fake read. Presence does not change admission, ack, or post eligibility."""
        _require_disabled()
        if type(key) is not str:
            self.manual_required = True
            return ReadReport("NOT_CHECKED", True, True, 0)
        digest = self._ack_digest(key)
        if digest is None:
            return ReadReport("NOT_CHECKED", False, False, 0)
        try:
            found = reader.read(key)
        except Exception:
            self.manual_required = True
            return ReadReport("NOT_CHECKED", True, True, 1)
        except BaseException:
            self.manual_required = True
            raise
        if found is not None and type(found) is not bytes:
            self.manual_required = True
            return ReadReport("NOT_CHECKED", True, True, 1)
        marker = acknowledgment_marker(digest)
        confirmation = "PRESENT" if found is not None and marker in found else "NOT_FOUND_AT_CHECK"
        wrote = self._write(
            "UPDATE outbox SET confirmation = ? WHERE delivery_key = ?",
            (confirmation, key),
            commit_readback,
        )
        if not wrote:
            self.manual_required = True
            return ReadReport("NOT_CHECKED", True, True, 1)
        return ReadReport(confirmation, False, False, 1)

    def _post_blocked(self) -> bool:
        connection = _connect(self.path)
        try:
            if _store_blocks(connection):
                return True
            row = connection.execute(
                "SELECT 1 FROM outbox WHERE post_state = 'ATTEMPTING' LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def _eligible_post(self):
        connection = _connect(self.path)
        try:
            rows = connection.execute(
                "SELECT delivery_key, ack_digest FROM outbox WHERE post_state = 'NOT_ATTEMPTED' "
                "ORDER BY delivery_key"
            ).fetchall()
            for row in rows:
                key = row["delivery_key"]
                try:
                    repository, thread_text, comment_text = key.split("\x1f")
                    thread, comment_id = int(thread_text), int(comment_text)
                except (ValueError, AttributeError):
                    continue
                delivery = connection.execute(
                    "SELECT state, result_json FROM deliveries "
                    "WHERE repository = ? AND thread = ? AND comment_id = ?",
                    (repository, thread, comment_id),
                ).fetchone()
                if delivery is None or delivery["state"] != "FINAL" or delivery["result_json"] is None:
                    continue
                try:
                    payload = json.loads(delivery["result_json"])
                except json.JSONDecodeError:
                    continue
                if (
                    type(payload) is dict
                    and payload.get("admission") == "ADMITTED"
                    and payload.get("ack") == "CONFIRMED_BY_SINK"
                    and not _blocks_payload(payload)
                ):
                    return key, bytes(row["ack_digest"]), repository, thread
            return None
        finally:
            connection.close()

    def _ack_digest(self, key: str):
        connection = _connect(self.path)
        try:
            row = connection.execute(
                "SELECT ack_digest FROM outbox WHERE delivery_key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return bytes(row["ack_digest"])
