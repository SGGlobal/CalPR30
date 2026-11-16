"""Synthetic fixtures in disposable SQLite files; no operational storage or network."""
import ast
import dataclasses
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3

import pytest

from skippy.collaboration import github_comment_observation_store as store
from skippy.collaboration import github_comment_read_session as session
from skippy.collaboration.github_live_provider import HTTPResponse

SECRET = "synthetic_untrusted_body_and_private_exception"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network or reader invoked by storage")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "observations.db")
    assert store.initialize_store(path, "fixture").outcome == "INITIALIZED"
    return path


def result(bodies=(SECRET,), *, thread=23, more=False, start_page=1):
    calls = []
    def fake(method, url, headers, timeout):
        calls.append((method, url, headers, timeout))
        page = start_page + len(calls) - 1
        metadata = {"content-type": "application/json"}
        if more:
            metadata["link"] = (f'<https://api.github.com/repos/SGGlobal/QLA/issues/{thread}/comments'
                                f'?per_page=25&page={page + 1}>; rel="next"')
        items = [{"id": i + 1, "body": b} for i, b in enumerate(bodies)]
        return HTTPResponse(200, json.dumps(items).encode(), metadata)
    hint = None if start_page == 1 else (
        f"https://api.github.com/repos/SGGlobal/QLA/issues/{thread}/comments?per_page=25&page={start_page}")
    answer = session.read_comment_session(fake, "SGGlobal/QLA", thread, start_token=hint)
    assert answer.observations is not None
    return answer


def reserve(path, attempt="attempt", thread=23, token=None):
    return store.reserve_attempt(path, "fixture", attempt, "SGGlobal/QLA", thread, token)


def inspect(path, attempt="attempt"):
    return store.inspect_attempt(path, "fixture", attempt)


def raw_counts(path):
    with sqlite3.connect(path) as connection:
        return (connection.execute("SELECT state FROM attempts ORDER BY attempt_id").fetchall(),
                connection.execute("SELECT count(*) FROM observations").fetchone()[0])


def test_empty_initialization_and_confirmed_reservation(db):
    assert store.inspect_store(db, "fixture") == store.StoreInspection("fixture", False, ())
    assert reserve(db).outcome == "RESERVED"
    assert inspect(db).state == "PENDING" and inspect(db).evidence is None
    assert store.inspect_store(db, "fixture").blocked is True
    with pytest.raises(store.StoreRefusal):
        store.read_observations(db, "fixture", "attempt")
    with pytest.raises(store.StoreRefusal):
        reserve(db, "another")
    assert raw_counts(db) == ([("PENDING",)], 0)


@pytest.mark.parametrize("body", [None, "", "\x00\r\n", "é", "e\u0301", "😀", "\ud800", "\udfff", SECRET])
def test_exact_round_trip_and_sanitized_inspection(db, body, capsys):
    reserve(db)
    value = result((body,))
    receipt = store.record_result(db, "fixture", "attempt", value)
    assert receipt.outcome == "RECORDED" and len(receipt.content_digest) == 64
    report = inspect(db)
    assert report.state == "RECORDED_ON_DISK"
    assert report.evidence.coverage == "PARTIAL_OR_UNKNOWN"
    assert report.content_digest == receipt.content_digest
    observations = store.read_observations(db, "fixture", "attempt")
    assert observations == value.observations and observations[0].body == body
    assert SECRET not in repr(report) + repr(receipt) + json.dumps(dataclasses.asdict(report))
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    with sqlite3.connect(db) as connection:
        tag, raw = connection.execute("SELECT body_tag,body FROM observations").fetchone()
        assert type(raw) is bytes and tag == int(body is not None)
        assert raw == (body.encode("utf-8", "surrogatepass") if body is not None else b"")


def test_empty_success_and_budget_stopped_hint_survive_reopen(db):
    reserve(db)
    store.record_result(db, "fixture", "attempt", result(()))
    assert store.read_observations(db, "fixture", "attempt") == ()
    token = "https://api.github.com/repos/SGGlobal/QLA/issues/26/comments?per_page=25&page=5"
    reserve(db, "second", 26, token)
    original = result((None, ""), thread=26, more=True, start_page=5)
    store.record_result(db, "fixture", "second", original)
    report = inspect(db, "second")
    assert report.evidence.outcome == "BUDGET_STOPPED"
    assert report.evidence.next_hint.endswith("&page=9")
    assert report.evidence.coverage == "PARTIAL_OR_UNKNOWN"
    assert report.evidence.duplicates == 6


def test_history_is_per_attempt_not_overwritten_or_globally_deduplicated(db):
    for name, body in [("A", "first"), ("a", "second"), ("third", "second")]:
        reserve(db, name)
        store.record_result(db, "fixture", name, result((body,)))
    assert raw_counts(db) == ([("RECORDED",)] * 3, 3)
    assert [store.read_observations(db, "fixture", name)[0].body for name in ("A", "a", "third")] == [
        "first", "second", "second"]


def test_identical_recording_replay_is_read_only_and_conflicting_replay_refuses(db, monkeypatch):
    reserve(db)
    value = result()
    receipt = store.record_result(db, "fixture", "attempt", value)
    def forbidden(*args, **kwargs):
        pytest.fail("replay attempted a write connection")
    monkeypatch.setattr(store, "_write", forbidden)
    replay = store.record_result(db, "fixture", "attempt", value)
    assert replay.outcome == "ALREADY_RECORDED" and replay.content_digest == receipt.content_digest
    with pytest.raises(store.StoreRefusal):
        store.record_result(db, "fixture", "attempt", result(("changed",)))
    assert raw_counts(db) == ([("RECORDED",)], 1)


@pytest.mark.parametrize("outcome,calls,pages", [("REFUSED", 0, 0), ("UNCERTAIN", 1, 0),
    ("INTERRUPTED", 2, 1), ("CONFLICT", 2, 2)])
def test_failed_sessions_record_held_metadata_only_and_block(db, outcome, calls, pages):
    reserve(db)
    evidence = session.SessionEvidence(outcome, calls, pages, pages * 2, 0, 0, 0)
    value = session.SessionResult(evidence, None)
    assert store.record_result(db, "fixture", "attempt", value).outcome == "HELD"
    assert inspect(db).state == "HELD" and inspect(db).evidence == evidence
    assert raw_counts(db) == ([("HELD",)], 0)
    with pytest.raises(store.StoreRefusal):
        reserve(db, "another")
    with pytest.raises(store.StoreRefusal):
        store.read_observations(db, "fixture", "attempt")


@pytest.mark.parametrize("kind", ["file", "empty", "directory", "symlink", "broken_symlink"])
def test_initialize_never_reuses_existing_path(tmp_path, kind):
    path = tmp_path / "existing"
    if kind in ("file", "empty"):
        path.write_bytes(b"existing" if kind == "file" else b"")
    elif kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target"
        if kind == "symlink":
            target.write_bytes(b"existing")
        path.symlink_to(target)
    with pytest.raises(store.StoreRefusal):
        store.initialize_store(str(path), "fixture")
    assert path.is_symlink() or path.exists()
    if kind in ("file", "empty"):
        assert path.read_bytes() == (b"existing" if kind == "file" else b"")


@pytest.mark.parametrize("operation", ["initialize", "reserve", "inspect"])
def test_version_floor_is_not_skipped(tmp_path, db, monkeypatch, operation):
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 45, 1))
    with pytest.raises(store.StoreRefusal):
        if operation == "initialize":
            store.initialize_store(str(tmp_path / "older.db"), "fixture")
        elif operation == "reserve":
            reserve(db)
        else:
            store.inspect_store(db, "fixture")
    assert raw_counts(db) == ([], 0)


@pytest.mark.parametrize("args", [ ("other/QLA", 23, None), ("SGGlobal/QLA", True, None),
    ("SGGlobal/QLA", "23", None), ("SGGlobal/QLA", 24, None),
    ("SGGlobal/QLA", 23, "https://api.github.com/repos/SGGlobal/QLA/issues/26/comments?per_page=25&page=2"),
    ("SGGlobal/QLA", 23, "https://api.github.com/repos/SGGlobal/QLA/issues/23/comments?page=2&per_page=25")])
def test_request_identity_refused_before_mutation(db, args):
    with pytest.raises(store.StoreRefusal):
        store.reserve_attempt(db, "fixture", "attempt", *args)
    assert raw_counts(db) == ([], 0)


@pytest.mark.parametrize("identifier", ["", "x" * 65, "é", "a/b", "a\n", 1, None])
def test_invalid_attempt_ids(db, identifier):
    with pytest.raises(store.StoreRefusal):
        reserve(db, identifier)
    assert raw_counts(db) == ([], 0)


def test_duplicate_reservation_never_grants_second_read(db):
    reserve(db)
    for name in ("attempt", "another"):
        with pytest.raises(store.StoreRefusal):
            reserve(db, name)
    assert inspect(db).state == "PENDING"


@pytest.mark.parametrize("change", [
    {"coverage": "COMPLETE"}, {"transport_calls": True}, {"transport_calls": 5},
    {"pages_validated": 0}, {"validated_bytes": 0}, {"entries_observed": 101},
    {"unique_comments": 2}, {"duplicates": 1}, {"validation_code": SECRET},
    {"next_hint": "https://evil.invalid/"}, {"outcome": "CONFLICT"},
    {"outcome": "BUDGET_STOPPED"},
])
def test_inconsistent_evidence_is_refused_without_record_mutation(db, change):
    reserve(db)
    value = result()
    value = dataclasses.replace(value, evidence=dataclasses.replace(value.evidence, **change))
    with pytest.raises(store.StoreRefusal):
        store.record_result(db, "fixture", "attempt", value)
    assert raw_counts(db) == ([("PENDING",)], 0)


@pytest.mark.parametrize("kind", ["none", "list", "duplicate", "thread", "repo", "id_bool", "id_overflow", "body"])
def test_invalid_observation_tuple_or_identity_refused(db, kind):
    reserve(db)
    value = result()
    original = value.observations[0]
    if kind == "none":
        observations = None
    elif kind == "list":
        observations = list(value.observations)
    elif kind == "duplicate":
        observations = (original, original)
    else:
        changes = {"thread": {"thread": 26}, "repo": {"repository": "other/QLA"},
                   "id_bool": {"comment_id": True}, "id_overflow": {"comment_id": 2**63},
                   "body": {"body": b"bytes"}}
        observations = (dataclasses.replace(original, **changes[kind]),)
    with pytest.raises(store.StoreRefusal):
        store.record_result(db, "fixture", "attempt", dataclasses.replace(value, observations=observations))
    assert raw_counts(db) == ([("PENDING",)], 0)


def test_oversized_body_no_truncation_or_mutation(db):
    reserve(db)
    item = session.CommentObservation("SGGlobal/QLA", 23, 1, "x" * 4_000_001)
    evidence = session.SessionEvidence("NO_NEXT_HINT", 4, 4, 4_000_000, 1, 1, 0)
    with pytest.raises(store.StoreRefusal):
        store.record_result(db, "fixture", "attempt", session.SessionResult(evidence, (item,)))
    assert raw_counts(db) == ([("PENDING",)], 0)


class ConnectionProxy:
    def __init__(self, connection, stage):
        self.connection, self.stage, self.inserted = connection, stage, 0

    def execute(self, sql, *args):
        if sql.startswith("INSERT INTO observations"):
            self.inserted += 1
            if self.stage == "second_insert" and self.inserted == 2:
                raise sqlite3.OperationalError(SECRET)
        if self.stage == "evidence_update" and sql.startswith("UPDATE attempts SET state"):
            raise sqlite3.OperationalError(SECRET)
        return self.connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("stage", ["second_insert", "evidence_update", "commit_before", "commit_after"])
def test_record_faults_no_partial_record_and_no_false_ack(db, monkeypatch, stage):
    reserve(db)
    value = result(("one", "two"))
    original_connect = store._connect
    original_commit = store._commit
    if stage in ("second_insert", "evidence_update"):
        def connect(*args, **kwargs):
            c = original_connect(*args, **kwargs)
            return ConnectionProxy(c, stage) if kwargs.get("write") else c
        monkeypatch.setattr(store, "_connect", connect)
    else:
        def commit(connection):
            if stage == "commit_after":
                original_commit(connection)
            raise sqlite3.OperationalError(SECRET)
        monkeypatch.setattr(store, "_commit", commit)
    with pytest.raises(store.StoreUncertain) as error:
        store.record_result(db, "fixture", "attempt", value)
    assert SECRET not in str(error.value)
    assert db in store._blocked
    expected = ([("RECORDED",)], 2) if stage == "commit_after" else ([("PENDING",)], 0)
    assert raw_counts(db) == expected
    report = inspect(db)
    assert report.state == ("RECORDED_ON_DISK" if stage == "commit_after" else "PENDING")
    with pytest.raises(store.StoreRefusal):
        reserve(db, "new")
    if stage == "commit_after":
        assert store.record_result(db, "fixture", "attempt", value).outcome == "ALREADY_RECORDED"


@pytest.mark.parametrize("landed", [False, True])
def test_reservation_commit_ambiguity_never_grants_permission(db, monkeypatch, landed):
    original = store._commit
    def commit(connection):
        if landed:
            original(connection)
        raise OSError(SECRET)
    monkeypatch.setattr(store, "_commit", commit)
    with pytest.raises(store.StoreUncertain):
        reserve(db)
    assert raw_counts(db) == ([("PENDING",)] if landed else [], 0)
    with pytest.raises(store.StoreRefusal):
        reserve(db, "second")


@pytest.mark.parametrize("stage", ["commit", "directory_sync"])
def test_initialization_fault_retains_file_and_stops_writer(tmp_path, monkeypatch, stage):
    path = str(tmp_path / "fault.db")
    def bad(*args):
        raise OSError(SECRET)
    monkeypatch.setattr(store, "_commit" if stage == "commit" else "_unused", bad, raising=False)
    if stage == "directory_sync":
        monkeypatch.setattr(store.os, "fsync", bad)
    with pytest.raises(store.StoreUncertain):
        store.initialize_store(path, "fixture")
    assert Path(path).exists() and path in store._blocked
    with pytest.raises(store.StoreRefusal):
        store.initialize_store(path, "fixture")


@pytest.mark.parametrize("sql", [
    "UPDATE store_meta SET store_id='wrong'",
    "UPDATE attempts SET reader_sha='wrong'",
    "UPDATE attempts SET request_digest=zeroblob(32)",
    "UPDATE attempts SET content_digest=zeroblob(32)",
    "UPDATE attempts SET evidence=CAST('{}' AS BLOB)",
    "UPDATE observations SET body=CAST('changed' AS BLOB)",
    "UPDATE observations SET body_digest=zeroblob(32)",
    "UPDATE observations SET ordinal=1",
    "DELETE FROM observations",
    "INSERT INTO observations SELECT 'orphan',ordinal,comment_id,body_tag,body,body_digest FROM observations",
    "CREATE TABLE unexpected (value TEXT)",
    "CREATE INDEX extra ON attempts(thread)",
    "CREATE TRIGGER extra AFTER INSERT ON attempts BEGIN SELECT 1; END",
])
def test_inconsistent_store_refuses_inspection_and_writes(db, sql):
    reserve(db)
    store.record_result(db, "fixture", "attempt", result())
    with sqlite3.connect(db) as connection:
        connection.execute(sql)
    with pytest.raises(store.StoreRefusal):
        store.inspect_store(db, "fixture")
    with pytest.raises(store.StoreRefusal):
        reserve(db, "new")


@pytest.mark.parametrize("variant", ["NOCASE", "WITHOUT_ROWID", "partial", "expression", "missing_constraint"])
def test_schema_variants_not_accepted(tmp_path, variant):
    path = str(tmp_path / "variant.db")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        for name, original in store._DDL.items():
            sql = original
            if name == "attempts":
                if variant == "NOCASE":
                    sql = sql.replace("COLLATE BINARY", "COLLATE NOCASE")
                elif variant == "WITHOUT_ROWID":
                    sql = sql.replace(") STRICT", ") STRICT, WITHOUT ROWID")
                elif variant == "missing_constraint":
                    sql = sql.replace(" CHECK(thread IN (23,26))", "")
            if name == "observations" and variant in ("partial", "expression"):
                sql = sql.replace(",\n UNIQUE(attempt_id,comment_id)", "")
            connection.execute(sql)
        connection.execute("INSERT INTO store_meta VALUES(1,1,'fixture','SGGlobal/QLA','23,26','utf8-surrogatepass-v1')")
        if variant == "partial":
            connection.execute("CREATE UNIQUE INDEX alternate ON observations(attempt_id,comment_id) WHERE comment_id>0")
        elif variant == "expression":
            connection.execute("CREATE UNIQUE INDEX alternate ON observations(attempt_id,comment_id+0)")
    with pytest.raises(store.StoreRefusal):
        store.inspect_store(path, "fixture")


def test_wrong_store_identity_missing_corrupt_and_readonly_open_never_create(db, tmp_path):
    with pytest.raises(store.StoreRefusal):
        store.inspect_store(db, "other")
    for name in ("missing.db", "corrupt.db", "zero.db"):
        p = tmp_path / name
        if name != "missing.db":
            p.write_bytes(b"junk" if name == "corrupt.db" else b"")
        with pytest.raises(store.StoreRefusal):
            store.inspect_store(str(p), "fixture")
        assert p.exists() == (name != "missing.db")


def test_connections_closed_on_fault_and_settings_read_back(db, monkeypatch):
    actual = store._connect
    seen = []
    def connect(*args, **kwargs):
        c = actual(*args, **kwargs)
        assert c.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert c.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        if kwargs.get("write"):
            assert c.execute("PRAGMA synchronous").fetchone() == (2,)
        seen.append(c)
        return c
    monkeypatch.setattr(store, "_connect", connect)
    reserve(db)
    with pytest.raises(store.StoreRefusal):
        reserve(db)
    for c in seen:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")


def test_inspection_no_write_or_reader_and_query_only_connection(db, monkeypatch):
    reserve(db)
    original = store._connect
    seen = []
    def connect(*args, **kwargs):
        assert not kwargs.get("write", False)
        c = original(*args, **kwargs)
        assert c.execute("PRAGMA query_only").fetchone() == (1,)
        c.set_trace_callback(seen.append)
        return c
    def forbidden(*args, **kwargs):
        pytest.fail("inspection attempted reader, commit or write")
    monkeypatch.setattr(store, "_connect", connect)
    monkeypatch.setattr(store, "_write", forbidden)
    monkeypatch.setattr(store, "_commit", forbidden)
    monkeypatch.setattr(session, "read_comment_session", forbidden)
    assert store.inspect_store(db, "fixture").blocked is True
    assert inspect(db).state == "PENDING"
    assert raw_counts(db) == ([("PENDING",)], 0)
    assert all(sql.split()[0].upper() in {"SELECT", "PRAGMA", "BEGIN"} for sql in seen)


def test_four_million_encoded_body_storage_boundary(db):
    reserve(db)
    observations = tuple(session.CommentObservation("SGGlobal/QLA", 23, n + 1, "x" * 1_000_000)
                         for n in range(4))
    evidence = session.SessionEvidence("NO_NEXT_HINT", 4, 4, 4_000_000, 4, 4, 0)
    value = session.SessionResult(evidence, observations)
    assert store.record_result(db, "fixture", "attempt", value).outcome == "RECORDED"
    assert store.read_observations(db, "fixture", "attempt") == observations


@pytest.mark.parametrize("error", [KeyboardInterrupt(SECRET), SystemExit(SECRET), store.StoreRefusal(SECRET)])
def test_all_commit_exceptions_fail_uncertain_even_after_landing(db, monkeypatch, error):
    reserve(db)
    original = store._commit
    def commit(connection):
        original(connection)
        raise error
    monkeypatch.setattr(store, "_commit", commit)
    with pytest.raises(store.StoreUncertain) as caught:
        store.record_result(db, "fixture", "attempt", result())
    assert SECRET not in str(caught.value)
    assert inspect(db).state == "RECORDED_ON_DISK" and db in store._blocked


def test_store_imports_no_operational_capabilities_or_main():
    source = Path(store.__file__).read_text()
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    assert set(names) == {"__future__", "hashlib", "json", "os", "re", "sqlite3", "stat",
                          "contextlib", "dataclasses", "pathlib",
                          "skippy.collaboration.github_comment_read_session"}
    assert "__main__" not in source


@pytest.mark.parametrize("name", ["journal_mode", "foreign_keys", "synchronous"])
def test_write_pragma_failure_closes_connection_without_mutation(db, monkeypatch, name):
    original = store.sqlite3.connect
    closed = []
    class BadPragma:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql, *args):
            if sql == f"PRAGMA {name}":
                raise sqlite3.OperationalError(SECRET)
            return self.connection.execute(sql, *args)
        def close(self):
            self.connection.close()
            closed.append(True)
    def connect(*args, **kwargs):
        return BadPragma(original(*args, **kwargs))
    with monkeypatch.context() as patch:
        patch.setattr(store.sqlite3, "connect", connect)
        with pytest.raises(store.StoreUncertain) as caught:
            reserve(db)
        assert SECRET not in str(caught.value)
    assert closed == [True] and raw_counts(db) == ([], 0)


def test_runtime_execution_flag_blocks_new_access(db, monkeypatch):
    monkeypatch.setattr(store, "execution_enabled", True)
    with pytest.raises(store.StoreRefusal):
        reserve(db)
    with pytest.raises(store.StoreRefusal):
        store.inspect_store(db, "fixture")


def _reserve_child(path, attempt, start, queue):
    start.wait(10)
    try:
        store.reserve_attempt(path, "fixture", attempt, "SGGlobal/QLA", 23)
        queue.put("reserved")
    except (store.StoreRefusal, store.StoreUncertain):
        queue.put("blocked")


@pytest.mark.parametrize("same_id", [False, True])
def test_process_reservations_only_one_permission(db, same_id):
    context = multiprocessing.get_context("spawn")
    queue, start = context.Queue(), context.Event()
    processes = [context.Process(target=_reserve_child,
        args=(db, "same" if same_id else f"attempt-{n}", start, queue)) for n in range(4)]
    try:
        for p in processes:
            p.start()
        start.set()
        outcomes = [queue.get(timeout=15) for _ in processes]
        for p in processes:
            p.join(10)
            assert p.exitcode == 0
        assert outcomes.count("reserved") == 1 and outcomes.count("blocked") == 3
        assert raw_counts(db) == ([("PENDING",)], 0)
    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join(5)
        queue.close()
        queue.join_thread()


def _crash_child(path, stage):
    if stage == "after_reserve":
        store.reserve_attempt(path, "fixture", "attempt", "SGGlobal/QLA", 23)
        os._exit(17)
    actual = store._commit
    def commit(connection):
        if stage == "after_record_commit":
            actual(connection)
        os._exit(17)
    store._commit = commit
    store.record_result(path, "fixture", "attempt", result(("one", "two")))


@pytest.mark.parametrize("stage", ["after_reserve", "before_record_commit", "after_record_commit"])
def test_process_crash_reopen_is_inspection_only(db, stage):
    if stage != "after_reserve":
        reserve(db)
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_crash_child, args=(db, stage))
    try:
        child.start()
        child.join(15)
        assert child.exitcode == 17
        report = inspect(db)
        assert report.state == ("RECORDED_ON_DISK" if stage == "after_record_commit" else "PENDING")
        expected = ([("RECORDED",)], 2) if stage == "after_record_commit" else ([("PENDING",)], 0)
        assert raw_counts(db) == expected
        assert raw_counts(db) == expected  # inspection performs no logical state transition
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
