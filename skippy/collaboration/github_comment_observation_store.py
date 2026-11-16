"""Offline observation custody only. Never invoke a reader, transport or work sink."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from skippy.collaboration.github_comment_read_session import (
    CommentObservation, SessionEvidence, SessionResult,
)

execution_enabled = False
READER_SHA = "aac231f4ef606c6e5ccc85f1dabbb2ff7c48327e"
MIN_SQLITE = (3, 51, 3)
_blocked: set[str] = set()  # Process-local stop, never a durable/global HALT claim.
_CODES = {"UNCLASSIFIED", "TRANSPORT", "RESPONSE", "BODY_BOUNDS", "CONTENT_TYPE",
          "JSON", "PAGE_STRUCTURE", "PAGINATION"}
_SUCCESS = {"NO_NEXT_HINT", "BUDGET_STOPPED"}
_FAILURE = {"REFUSED", "UNCERTAIN", "INTERRUPTED", "CONFLICT"}
_DDL = {
    "store_meta": """CREATE TABLE store_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 version INTEGER NOT NULL CHECK(version=1),
 store_id TEXT COLLATE BINARY NOT NULL,
 repository TEXT NOT NULL CHECK(repository='SGGlobal/QLA'),
 threads TEXT NOT NULL CHECK(threads='23,26'),
 codec TEXT NOT NULL CHECK(codec='utf8-surrogatepass-v1')) STRICT""",
    "attempts": """CREATE TABLE attempts (
 attempt_id TEXT COLLATE BINARY PRIMARY KEY NOT NULL,
 repository TEXT NOT NULL CHECK(repository='SGGlobal/QLA'),
 thread INTEGER NOT NULL CHECK(thread IN (23,26)),
 start_token TEXT,
 reader_sha TEXT NOT NULL,
 request_digest BLOB NOT NULL CHECK(length(request_digest)=32),
 state TEXT NOT NULL CHECK(state IN ('PENDING','RECORDED','HELD')),
 evidence BLOB,
 content_digest BLOB,
 CHECK((state='PENDING' AND evidence IS NULL AND content_digest IS NULL) OR
       (state IN ('RECORDED','HELD') AND evidence IS NOT NULL AND length(content_digest)=32))) STRICT""",
    "observations": """CREATE TABLE observations (
 attempt_id TEXT COLLATE BINARY NOT NULL REFERENCES attempts(attempt_id),
 ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 99),
 comment_id INTEGER NOT NULL CHECK(comment_id>0),
 body_tag INTEGER NOT NULL CHECK(body_tag IN (0,1)),
 body BLOB NOT NULL,
 body_digest BLOB NOT NULL CHECK(length(body_digest)=32),
 CHECK(body_tag=1 OR length(body)=0),
 PRIMARY KEY(attempt_id,ordinal),
 UNIQUE(attempt_id,comment_id)) STRICT""",
}
_INDEXES = {
    "attempts": {"sqlite_autoindex_attempts_1": ("pk", ("attempt_id",))},
    "observations": {
        "sqlite_autoindex_observations_1": ("pk", ("attempt_id", "ordinal")),
        "sqlite_autoindex_observations_2": ("u", ("attempt_id", "comment_id")),
    },
    "store_meta": {},
}


class StoreRefusal(ValueError):
    """Static refusal; never includes a body, foreign exception, path or credential."""


class StoreUncertain(RuntimeError):
    """The current writer is stopped; inspect with the separate read-only API."""


@dataclass(frozen=True)
class Receipt:
    outcome: str
    attempt_id: str
    content_digest: str | None = None


@dataclass(frozen=True)
class AttemptInspection:
    attempt_id: str
    thread: int
    state: str
    evidence: SessionEvidence | None
    content_digest: str | None


@dataclass(frozen=True)
class StoreInspection:
    store_id: str
    blocked: bool
    attempts: tuple[AttemptInspection, ...]


def _require(condition):
    if not condition:
        raise StoreRefusal("observation store refused")


def _runtime():
    _require(execution_enabled is False and sqlite3.sqlite_version_info >= MIN_SQLITE)


def _identifier(value):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value, re.ASCII))


def _path(path, *, existing=True):
    _require(type(path) is str and Path(path).is_absolute())
    p = Path(path)
    _require(p.resolve() == p and p.parent.is_dir())
    if existing:
        info = p.lstat()
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    return p


def _hint(value, thread):
    if value is None:
        return
    _require(type(value) is str and re.fullmatch(
        re.escape(f"https://api.github.com/repos/SGGlobal/QLA/issues/{thread}/comments") +
        r"\?per_page=25&page=([1-9][0-9]{0,3})", value, re.ASCII))


def _request(store_id, attempt_id, repository, thread, token):
    _identifier(store_id)
    _identifier(attempt_id)
    _require(type(repository) is str and repository == "SGGlobal/QLA")
    _require(type(thread) is int and thread in (23, 26))
    _hint(token, thread)
    return (attempt_id, repository, thread, token, READER_SHA)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _digest(*parts):
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.digest()


def _request_digest(store_id, request):
    return _digest(b"observation-request-v1", _json([store_id, *request]))


def _body(tag, raw):
    _require(type(tag) is int and tag in (0, 1) and type(raw) is bytes)
    _require(tag == 1 or raw == b"")
    value = raw.decode("utf-8", "surrogatepass") if tag else None
    _require(value is None or value.encode("utf-8", "surrogatepass") == raw)
    return value


def _validated_result(result, request):
    _require(type(result) is SessionResult and type(result.evidence) is SessionEvidence)
    evidence = result.evidence
    _require(type(evidence.outcome) is str and evidence.outcome in _SUCCESS | _FAILURE)
    _require(type(evidence.coverage) is str and evidence.coverage == "PARTIAL_OR_UNKNOWN")
    _require(type(evidence.validation_code) is str and evidence.validation_code in _CODES)
    for name, upper in (("transport_calls", 4), ("pages_validated", 4),
                        ("validated_bytes", 4_000_000), ("entries_observed", 100),
                        ("unique_comments", 100), ("duplicates", 100)):
        value = getattr(evidence, name)
        _require(type(value) is int and 0 <= value <= upper)
    calls, pages = evidence.transport_calls, evidence.pages_validated
    _require(pages <= calls and pages * 2 <= evidence.validated_bytes <= pages * 1_000_000)
    _require(evidence.entries_observed <= pages * 25)
    _require(evidence.unique_comments + evidence.duplicates <= evidence.entries_observed)
    _hint(evidence.next_hint, request[2])
    success = evidence.outcome in _SUCCESS
    rows = []
    if success:
        _require(type(result.observations) is tuple and 1 <= pages == calls <= 4)
        _require(evidence.validation_code == "UNCLASSIFIED")
        _require(len(result.observations) == evidence.unique_comments)
        _require(evidence.unique_comments + evidence.duplicates == evidence.entries_observed)
        if evidence.outcome == "BUDGET_STOPPED":
            _require(calls == 4 and evidence.next_hint is not None)
            start = int(request[3].rsplit("=", 1)[1]) if request[3] else 1
            _require(evidence.next_hint.endswith("&page=" + str(start + 4)))
        else:
            _require(evidence.next_hint is None)
        seen, total = set(), 0
        for ordinal, item in enumerate(result.observations):
            _require(type(item) is CommentObservation)
            _require(type(item.repository) is str and item.repository == request[1])
            _require(type(item.thread) is int and item.thread == request[2])
            _require(type(item.comment_id) is int and 0 < item.comment_id <= 9223372036854775807)
            _require(item.comment_id not in seen and (item.body is None or type(item.body) is str))
            seen.add(item.comment_id)
            tag = int(item.body is not None)
            raw = item.body.encode("utf-8", "surrogatepass") if tag else b""
            total += len(raw)
            _require(total <= 4_000_000)
            rows.append((ordinal, item.comment_id, tag, raw,
                         _digest(b"observation-body-v1", bytes([tag]), raw)))
        _require(total <= evidence.validated_bytes)
    else:
        _require(result.observations is None and evidence.next_hint is None)
        if evidence.outcome == "REFUSED":
            _require(calls == pages == evidence.validated_bytes == evidence.entries_observed == 0)
    raw_evidence = _json(asdict(evidence))
    _require(len(raw_evidence) <= 8192)
    return raw_evidence, rows, "RECORDED" if success else "HELD"


def _batch_digest(request_digest, evidence, rows):
    parts = [b"observation-batch-v1", request_digest, evidence]
    for ordinal, comment_id, tag, raw, digest in rows:
        parts.extend((_json([ordinal, comment_id, tag]), raw, digest))
    return _digest(*parts)


def _schema(connection):
    objects = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name").fetchall()
    expected = [("table", name, name, sql) for name, sql in _DDL.items()]
    expected += [("index", name, table, None) for table, indexes in _INDEXES.items()
                 for name in indexes]
    _require(sorted(objects, key=lambda row: row[1]) == sorted(expected, key=lambda row: row[1]))
    tables = {row[1]: row for row in connection.execute("PRAGMA table_list") if row[0] == "main"}
    for table in _DDL:
        _require(tables[table][2] == "table" and tables[table][4:] == (0, 1))
        indexes = connection.execute(f"PRAGMA index_list('{table}')").fetchall()
        _require({r[1] for r in indexes} == set(_INDEXES[table]))
        for _, name, unique, origin, partial in indexes:
            expected_origin, columns = _INDEXES[table][name]
            _require(unique == 1 and origin == expected_origin and partial == 0)
            keys = [r for r in connection.execute(f"PRAGMA index_xinfo('{name}')") if r[5]]
            _require(tuple(r[2] for r in keys) == columns)
            _require(all(r[1] >= 0 and r[3] == 0 and r[4] == "BINARY" for r in keys))
    _require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)])
    _require(connection.execute("PRAGMA foreign_key_check").fetchall() == [])


def _load_unchecked(connection, store_id):
    _schema(connection)
    _require(connection.execute("SELECT * FROM store_meta").fetchall() ==
             [(1, 1, store_id, "SGGlobal/QLA", "23,26", "utf8-surrogatepass-v1")])
    attempts = {}
    for row in connection.execute("SELECT * FROM attempts ORDER BY attempt_id"):
        attempt_id, repository, thread, token, source, request_digest, state, evidence, content = row
        request = _request(store_id, attempt_id, repository, thread, token)
        _require(source == READER_SHA and request_digest == _request_digest(store_id, request))
        stored = connection.execute(
            "SELECT ordinal,comment_id,body_tag,body,body_digest FROM observations "
            "WHERE attempt_id=? ORDER BY ordinal LIMIT 101", (attempt_id,)).fetchall()
        _require(len(stored) <= 100)
        if state == "PENDING":
            _require(evidence is None and content is None and stored == [])
            result = None
        else:
            _require(state in ("HELD", "RECORDED") and type(evidence) is bytes and len(evidence) <= 8192)
            value = json.loads(evidence)
            _require(type(value) is dict and _json(value) == evidence)
            info = SessionEvidence(**value)
            observations = tuple(CommentObservation(repository, thread, r[1], _body(r[2], r[3]))
                                 for r in stored) if state == "RECORDED" else None
            result = SessionResult(info, observations)
            encoded, wanted, wanted_state = _validated_result(result, request)
            _require(state == wanted_state and stored == wanted and encoded == evidence)
            _require(content == _batch_digest(request_digest, evidence, stored))
        attempts[attempt_id] = (request, request_digest, state, result, content, stored)
    _require(sum(item[2] in ("PENDING", "HELD") for item in attempts.values()) <= 1)
    return attempts


def _load(connection, store_id):
    try:
        return _load_unchecked(connection, store_id)
    except (ValueError, TypeError, UnicodeError, KeyError, IndexError, RecursionError):
        raise StoreRefusal("store consistency refused") from None


def _connect(path, *, write=False, initialize=False):
    connection = sqlite3.connect(Path(path).as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=0.25, isolation_level=None)
    try:
        if initialize:
            _require(connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",))
        else:
            _require(connection.execute("PRAGMA journal_mode").fetchone() == ("wal",))
        connection.execute("PRAGMA foreign_keys=ON")
        _require(connection.execute("PRAGMA foreign_keys").fetchone() == (1,))
        connection.execute("PRAGMA busy_timeout=250")
        if write:
            connection.execute("PRAGMA synchronous=FULL")
            _require(connection.execute("PRAGMA synchronous").fetchone() == (2,))
        else:
            connection.execute("PRAGMA query_only=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _commit(connection):
    """One transaction-commit seam for synthetic fault injection."""
    connection.commit()


@contextmanager
def _read(path, store_id):
    connection = None
    try:
        _runtime()
        _identifier(store_id)
        _path(path)
        connection = _connect(path)
        connection.execute("BEGIN")
        yield connection, _load(connection, store_id)
    except (Exception, KeyboardInterrupt, SystemExit):
        raise StoreRefusal("inspection refused") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except (Exception, KeyboardInterrupt, SystemExit):
                raise StoreRefusal("inspection refused") from None


@contextmanager
def _write(path, store_id):
    connection = None
    committing = False
    try:
        _runtime()
        _identifier(store_id)
        _path(path)
        _require(path not in _blocked)
        connection = _connect(path, write=True)
        connection.execute("BEGIN IMMEDIATE")
        yield connection, _load(connection, store_id)
        committing = True
        _commit(connection)
    except StoreRefusal:
        if committing:
            _blocked.add(path)
            raise StoreUncertain("storage outcome uncertain; writer stopped") from None
        raise StoreRefusal("write refused") from None
    except (Exception, KeyboardInterrupt, SystemExit):
        _blocked.add(path)
        raise StoreUncertain("storage outcome uncertain; writer stopped") from None
    finally:
        if connection is not None:
            try:
                connection.close()  # Uncommitted work is rolled back, never retried.
            except (Exception, KeyboardInterrupt, SystemExit):
                _blocked.add(path)
                raise StoreUncertain("storage outcome uncertain; writer stopped") from None


def initialize_store(path: str, store_id: str) -> Receipt:
    """Explicit create-only initialization; failure retains any created file."""
    connection = None
    created = False
    try:
        _runtime()
        _identifier(store_id)
        p = _path(path, existing=False)
        _require(path not in _blocked)
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        created = True
        os.close(fd)
        connection = _connect(path, write=True, initialize=True)
        connection.execute("BEGIN IMMEDIATE")
        for sql in _DDL.values():
            connection.execute(sql)
        connection.execute("INSERT INTO store_meta VALUES (1,1,?,'SGGlobal/QLA','23,26',?)",
                           (store_id, "utf8-surrogatepass-v1"))
        _load(connection, store_id)
        _commit(connection)
        connection.close()
        connection = None
        directory_fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return Receipt("INITIALIZED", store_id)
    except (Exception, KeyboardInterrupt, SystemExit):
        if created:
            _blocked.add(path)
            raise StoreUncertain("initialization uncertain; writer stopped") from None
        raise StoreRefusal("initialization refused") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except (Exception, KeyboardInterrupt, SystemExit):
                _blocked.add(path)
                raise StoreUncertain("initialization uncertain; writer stopped") from None


def reserve_attempt(path: str, store_id: str, attempt_id: str, repository: str,
                    thread: int, start_token: str | None = None) -> Receipt:
    request = _request(store_id, attempt_id, repository, thread, start_token)
    with _write(path, store_id) as (connection, attempts):
        _require(attempt_id not in attempts)
        _require(not any(a[2] in ("PENDING", "HELD") for a in attempts.values()))
        connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,'PENDING',NULL,NULL)",
                           (*request, _request_digest(store_id, request)))
    return Receipt("RESERVED", attempt_id)


def record_result(path: str, store_id: str, attempt_id: str, result: SessionResult) -> Receipt:
    _identifier(attempt_id)
    try:
        with _read(path, store_id) as (_, attempts):
            _require(attempt_id in attempts)
            request, request_digest, state, _, _, _ = attempts[attempt_id]
        evidence, rows, wanted_state = _validated_result(result, request)
        content = _batch_digest(request_digest, evidence, rows)
    except (Exception, KeyboardInterrupt, SystemExit):
        raise StoreRefusal("result refused") from None
    # A matching replay is inspection only, including after a commit/ack ambiguity.
    if state == "RECORDED":
        with _read(path, store_id) as (_, attempts):
            prior = attempts[attempt_id]
            _require(prior[0] == request and prior[2] == state and prior[4] == content and prior[5] == rows)
            _require(asdict(prior[3].evidence) == asdict(result.evidence))
        return Receipt("ALREADY_RECORDED", attempt_id, content.hex())
    with _write(path, store_id) as (connection, attempts):
        _require(attempt_id in attempts and attempts[attempt_id][0] == request)
        _require(attempts[attempt_id][2] == "PENDING")
        for row in rows:
            connection.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", (attempt_id, *row))
        connection.execute("UPDATE attempts SET state=?,evidence=?,content_digest=? WHERE attempt_id=?",
                           (wanted_state, evidence, content, attempt_id))
        _load(connection, store_id)
    return Receipt(wanted_state, attempt_id, content.hex())


def _inspection(attempt_id, item):
    request, _, state, result, digest, _ = item
    return AttemptInspection(attempt_id, request[2],
                             "RECORDED_ON_DISK" if state == "RECORDED" else state,
                             result.evidence if result else None,
                             digest.hex() if digest else None)


def inspect_store(path: str, store_id: str) -> StoreInspection:
    with _read(path, store_id) as (_, attempts):
        return StoreInspection(store_id, any(a[2] in ("PENDING", "HELD") for a in attempts.values()),
                               tuple(_inspection(key, value) for key, value in attempts.items()))


def inspect_attempt(path: str, store_id: str, attempt_id: str) -> AttemptInspection:
    _identifier(attempt_id)
    with _read(path, store_id) as (_, attempts):
        _require(attempt_id in attempts)
        return _inspection(attempt_id, attempts[attempt_id])


def read_observations(path: str, store_id: str, attempt_id: str) -> tuple[CommentObservation, ...]:
    _identifier(attempt_id)
    with _read(path, store_id) as (_, attempts):
        _require(attempt_id in attempts and attempts[attempt_id][2] == "RECORDED")
        return attempts[attempt_id][3].observations
