"""BRIDGE-02B.2.2 offline transport fixture (J13.6).

OFFLINE SYNTHETIC ONLY. NON-EXECUTING. FAIL-CLOSED.

Work order https://github.com/SGGlobal/QLA/issues/23#issuecomment-6873540007.
The governing contract is J13.6 of docs/bridge-02b2-compatibility-contract.md.

This module is a disposable fixture. It does not open a remote connection, read
secrets, post to a carrier, or schedule work. Signed admission stays with the
unchanged ingress ``deliver`` path. ``execution_enabled`` remains False.

P3 (this revision): bounded fake pages, exact frame extraction, and delivery
identity (body digest, duplicate collapse, contradictory-body hold). Later
priorities add the disposable journal and the fake poster. Nothing here is a
live transport or a claim of source completeness.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

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
