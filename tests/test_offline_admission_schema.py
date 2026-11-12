"""BRIDGE-02B.2.1 admission store schema preflight and locked initialization tests (F).

Synthetic. Offline. Non-executing. execution_enabled remains False.

I1 read-only preflight, I2 writable connection + BEGIN IMMEDIATE, I3 recheck under the
lock, I4 WAL switch outside the transaction, I5 _block_if_unresolved(). Existing
evidence is never altered, dropped or migrated. I3 and I4 are not atomic together;
test_f8 documents the remaining post-construction limitation.
"""

import hashlib
import os
import sqlite3
import threading

import pytest

import skippy.collaboration.offline_admission_gate as gate_module
from skippy.collaboration.offline_admission_gate import AdmissionError, OfflineAdmissionGate
from test_offline_admission_gate import ClearHalt, _key_pair, _now, _trust_root
from test_offline_admission_authorization import SpyEvidence, _msg

V2_TABLE = """
CREATE TABLE reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    role TEXT NOT NULL,
    payload_digest BLOB NOT NULL,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""
UQ_MESSAGE = "CREATE UNIQUE INDEX uq_message ON reservations(project_id, task_id, message_id)"
UQ_IDEM = "CREATE UNIQUE INDEX uq_idem ON reservations(project_id, task_id, idempotency_key)"

# Base (57f0ff8) v1 table: 12 columns, no recipient/role, user_version 0.
V1_TABLE = """
CREATE TABLE reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    sender TEXT NOT NULL,
    payload_digest BLOB NOT NULL,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""
V1_ROW = (
    "INSERT INTO reservations (project_id, task_id, checkpoint_id, assignment_id, message_id,"
    " idempotency_key, sender, payload_digest, sequence, state, recorded_at) VALUES"
    " ('QLA','t1','c1','a1','m1','i1','chief',x'00',1,'RESERVED','2026-11-08T12:00:00')"
)
V2_ROW = (
    "INSERT INTO reservations (project_id, task_id, checkpoint_id, assignment_id, message_id,"
    " idempotency_key, sender, recipient, role, payload_digest, sequence, state, recorded_at)"
    " VALUES ('QLA','t1','c1','a1','m1','i1','chief','jason','chief',x'00',1,'RESERVED',"
    "'2026-11-08T12:00:00')"
)


def _build(path, statements, journal_mode="delete"):
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA journal_mode={journal_mode}")
        with connection:
            for statement in statements:
                connection.execute(statement)
    finally:
        connection.close()


def _v2(*extra, version=2, table=V2_TABLE, indexes=(UQ_MESSAGE, UQ_IDEM), rows=(V2_ROW,)):
    return [table, *indexes, *rows, *extra, f"PRAGMA user_version = {version}"]


def _construct(path, spy=None):
    return OfflineAdmissionGate(
        path, _trust_root(_key_pair()[1]), spy or SpyEvidence(), ClearHalt()
    )


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ro(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _journal_mode(path):
    connection = _ro(path)
    try:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()


def _logical_state(path):
    connection = _ro(path)
    try:
        master = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        tables = [r[1] for r in master if r[0] == "table" and r[1] != "sqlite_sequence"]
        rows = {t: connection.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    return master, rows, version


class _ConnectRecorder:
    """Delegating sqlite3 replacement inside the gate module; records connect() args."""

    def __init__(self, real, wrap=None):
        self._real = real
        self._wrap = wrap
        self.calls = []
        self.opened = []

    def connect(self, *args, **kwargs):
        connection = self._real.connect(*args, **kwargs)
        self.calls.append((args, kwargs))
        self.opened.append(connection)
        return self._wrap(connection) if self._wrap else connection

    def __getattr__(self, name):
        return getattr(self._real, name)


def _all_closed(connections):
    for connection in connections:
        try:
            connection.total_changes
        except sqlite3.ProgrammingError:
            continue
        return False
    return True


@pytest.fixture
def recorder(monkeypatch):
    rec = _ConnectRecorder(sqlite3)
    monkeypatch.setattr(gate_module, "sqlite3", rec)
    return rec


# --- F1. read-only preflight rejections ------------------------------------

_F1_CASES = {
    "v1 database with rows": [V1_TABLE, UQ_MESSAGE, UQ_IDEM, V1_ROW],
    "v1 structure with user_version 2": [V1_TABLE, UQ_MESSAGE, UQ_IDEM, V1_ROW,
                                         "PRAGMA user_version = 2"],
    "composite superset index": _v2(indexes=(
        "CREATE UNIQUE INDEX uq_message ON reservations(project_id, task_id, message_id, sender)",
        UQ_IDEM,
    )),
    "partial index": _v2(indexes=(UQ_MESSAGE + " WHERE state = 'RESERVED'", UQ_IDEM)),
    "nocase index": _v2(indexes=(
        "CREATE UNIQUE INDEX uq_message ON reservations"
        "(project_id, task_id, message_id COLLATE NOCASE)",
        UQ_IDEM,
    )),
    "expression index": _v2(indexes=(
        "CREATE UNIQUE INDEX uq_message ON reservations(project_id, task_id, lower(message_id))",
        UQ_IDEM,
    )),
    "descending key": _v2(indexes=(
        "CREATE UNIQUE INDEX uq_message ON reservations(project_id, task_id, message_id DESC)",
        UQ_IDEM,
    )),
    "non-unique index": _v2(indexes=(
        "CREATE INDEX uq_message ON reservations(project_id, task_id, message_id)",
        UQ_IDEM,
    )),
    "same-named index wrong columns": _v2(indexes=(
        UQ_MESSAGE,
        "CREATE UNIQUE INDEX uq_idem ON reservations(project_id, task_id, message_id)",
    )),
    "swapped index names": _v2(indexes=(
        "CREATE UNIQUE INDEX uq_message ON reservations(project_id, task_id, idempotency_key)",
        "CREATE UNIQUE INDEX uq_idem ON reservations(project_id, task_id, message_id)",
    )),
    "missing index": _v2(indexes=(UQ_MESSAGE,)),
    "extra index": _v2("CREATE INDEX extra_idx ON reservations(sequence)"),
    "missing column": _v2(table=V2_TABLE.replace("    role TEXT NOT NULL,\n", ""), rows=()),
    "extra column": _v2(
        table=V2_TABLE.replace("    recorded_at TEXT NOT NULL\n",
                               "    recorded_at TEXT NOT NULL,\n    extra TEXT\n"),
    ),
    "wrong not null": _v2(table=V2_TABLE.replace("recipient TEXT NOT NULL", "recipient TEXT")),
    "wrong declared type": _v2(table=V2_TABLE.replace("role TEXT NOT NULL", "role BLOB NOT NULL")),
    "column default": _v2(
        table=V2_TABLE.replace("role TEXT NOT NULL", "role TEXT NOT NULL DEFAULT 'chief'"),
    ),
    "no autoincrement": _v2(
        table=V2_TABLE.replace("PRIMARY KEY AUTOINCREMENT", "PRIMARY KEY"),
    ),
    "unexpected trigger": _v2(
        "CREATE TRIGGER t_extra AFTER INSERT ON reservations BEGIN SELECT 1; END"
    ),
    "unexpected view": _v2("CREATE VIEW v_extra AS SELECT * FROM reservations"),
    "unexpected table": _v2("CREATE TABLE other (x)"),
    "user_version 1 on v2 structure": _v2(version=1),
    "user_version 3 on v2 structure": _v2(version=3),
    "empty schema non-empty file": ["PRAGMA user_version = 0", "CREATE TABLE tmp (x)",
                                    "DROP TABLE tmp"],
}


@pytest.mark.parametrize("label", list(_F1_CASES))
def test_f1_incompatible_store_rejected_read_only(tmp_path, recorder, label):
    path = tmp_path / "admit.db"
    _build(path, _F1_CASES[label])
    digest = _sha256(path)
    state = _logical_state(path)
    with pytest.raises(AdmissionError, match="^migration required"):
        _construct(path)
    assert _sha256(path) == digest
    assert _journal_mode(path) == "delete"
    assert not (tmp_path / "admit.db-wal").exists()
    assert _logical_state(path) == state
    # Only the read-only preflight connection was opened, and it was closed.
    assert len(recorder.calls) == 1
    args, kwargs = recorder.calls[0]
    assert kwargs.get("uri") is True and str(args[0]).endswith("?mode=ro")
    assert _all_closed(recorder.opened)
    os.remove(path)  # unlinkable on Windows only if no handle is left open


def test_f1_non_sqlite_file_rejected_without_writes(tmp_path, recorder):
    path = tmp_path / "admit.db"
    path.write_bytes(b"this is not an sqlite database" * 200)
    digest = _sha256(path)
    with pytest.raises(AdmissionError) as excinfo:
        _construct(path)
    assert "preflight" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, sqlite3.Error)
    assert _sha256(path) == digest
    assert len(recorder.calls) == 1 and recorder.calls[0][1].get("uri") is True
    assert _all_closed(recorder.opened)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["admit.db"]
    os.remove(path)


def test_f1_zero_byte_file_is_fresh(tmp_path):
    path = tmp_path / "admit.db"
    path.write_bytes(b"")
    _construct(path)
    assert _journal_mode(path) == "wal"
    assert _logical_state(path)[2] == 2


# --- F2. fresh database created under the lock -----------------------------


def test_f2_fresh_database_created_under_lock_then_wal(tmp_path, monkeypatch):
    statements = []

    def traced(connection):
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(gate_module, "sqlite3", _ConnectRecorder(sqlite3, wrap=traced))
    path = tmp_path / "admit.db"
    _construct(path)
    normalized = [" ".join(s.split()).upper() for s in statements]

    def first(prefix):
        return next(i for i, s in enumerate(normalized) if s.startswith(prefix))

    assert first("BEGIN IMMEDIATE") < first("CREATE TABLE RESERVATIONS")
    assert first("CREATE TABLE RESERVATIONS") < first("PRAGMA USER_VERSION = 2")
    assert first("PRAGMA USER_VERSION = 2") < first("COMMIT")
    assert first("COMMIT") < first("PRAGMA JOURNAL_MODE=WAL")
    master, rows, version = _logical_state(path)
    assert version == 2
    assert rows == {"reservations": []}
    assert {(m[0], m[1]) for m in master} == {
        ("table", "reservations"), ("table", "sqlite_sequence"),
        ("index", "uq_message"), ("index", "uq_idem"),
    }
    assert _journal_mode(path) == "wal"


def test_f2_compatible_existing_delete_database_accepted_and_switched(tmp_path):
    path = tmp_path / "admit.db"
    _build(path, _v2())
    state = _logical_state(path)
    _construct(path)
    assert _logical_state(path) == state
    assert _journal_mode(path) == "wal"


# --- F3 / F4. races between I1 and I2 --------------------------------------


def _hook(monkeypatch, action):
    monkeypatch.setattr(OfflineAdmissionGate, "_before_locked_init", lambda self: action(self))


def test_f3_fresh_database_race_rejected_under_lock(tmp_path, monkeypatch, recorder):
    path = tmp_path / "admit.db"

    def create_intruder(_gate):
        _build(path, ["CREATE TABLE intruder (x)", "INSERT INTO intruder VALUES (1)"])

    _hook(monkeypatch, create_intruder)
    with pytest.raises(AdmissionError, match="^migration required"):
        _construct(path)
    master, rows, version = _logical_state(path)
    assert [m[1] for m in master] == ["intruder"]
    assert rows == {"intruder": [(1,)]}
    assert version == 0
    assert _journal_mode(path) == "delete"
    assert not (tmp_path / "admit.db-wal").exists()
    assert _all_closed(recorder.opened)


def test_f4_compatible_delete_database_race_rejected_before_wal(tmp_path, monkeypatch, recorder):
    path = tmp_path / "admit.db"
    _build(path, _v2())
    assert _journal_mode(path) == "delete"

    def add_partial_index(_gate):
        _build(path, ["CREATE INDEX injected_partial ON reservations(sequence) "
                      "WHERE state = 'RESERVED'"])

    _hook(monkeypatch, add_partial_index)
    with pytest.raises(AdmissionError, match="^migration required"):
        _construct(path)
    master, rows, _version = _logical_state(path)
    assert "injected_partial" in [m[1] for m in master]
    assert len(rows["reservations"]) == 1
    assert _journal_mode(path) == "delete"
    assert not (tmp_path / "admit.db-wal").exists()
    assert _all_closed(recorder.opened)


def test_f4_existing_compatible_wal_database_remains_wal(tmp_path):
    path = tmp_path / "admit.db"
    _build(path, _v2(), journal_mode="wal")
    assert _journal_mode(path) == "wal"
    state = _logical_state(path)
    gate = _construct(path)
    assert _journal_mode(path) == "wal"
    assert _logical_state(path) == state
    env = _msg("assignment", message_id="m9", idempotency_key="i9", task_id="t9")
    assert gate.reserve(env, _now()).outcome == "reserved"


# --- F5. concurrent initializers -------------------------------------------


def _integrity_and_schema(path):
    connection = _ro(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        master = sorted(
            (r[0], r[1]) for r in connection.execute("SELECT type, name FROM sqlite_master")
        )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    return integrity, master, version


_EXPECTED_MASTER = sorted([
    ("index", "uq_idem"), ("index", "uq_message"),
    ("table", "reservations"), ("table", "sqlite_sequence"),
])


def run_concurrent_fresh_initialization(path, monkeypatch):
    """Two initializers race on one fresh path. Returns (successes, admission_errors).

    Each initializer must either succeed or raise a controlled AdmissionError; any other
    exception type fails. Afterwards the store must be intact (integrity_check ok,
    exact v2 schema or nothing, no partial state), every gate-opened connection must be
    closed, and a subsequent single initialization must succeed.
    """
    rec = _ConnectRecorder(sqlite3)
    monkeypatch.setattr(gate_module, "sqlite3", rec)
    barrier = threading.Barrier(2)
    outcomes = []

    def build():
        barrier.wait()
        try:
            _construct(path)
            outcomes.append("ok")
        except AdmissionError as exc:
            outcomes.append(("admission_error", str(exc)))
        except BaseException as exc:  # noqa: BLE001 - any other type is a failure
            outcomes.append(("unexpected", repr(exc)))

    threads = [threading.Thread(target=build) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 2
    assert [o for o in outcomes if o != "ok" and o[0] != "admission_error"] == []
    for outcome in outcomes:
        if outcome != "ok":
            assert outcome[1].startswith("storage failure during initialization")
    assert _all_closed(rec.opened), "every connection opened by the gate must be closed"

    integrity, master, version = _integrity_and_schema(path)
    assert integrity == [("ok",)]
    if outcomes.count("ok") == 0 and master == []:
        assert version == 0  # nothing committed: no partial state
    else:
        assert master == _EXPECTED_MASTER
        assert version == 2

    # A subsequent single initialization succeeds and leaves a WAL v2 store.
    _construct(path)
    integrity, master, version = _integrity_and_schema(path)
    assert integrity == [("ok",)]
    assert master == _EXPECTED_MASTER
    assert version == 2
    assert _journal_mode(path) == "wal"
    assert _all_closed(rec.opened)
    return outcomes.count("ok"), 2 - outcomes.count("ok")


@pytest.mark.parametrize("attempt", range(5))
def test_f5_two_gates_initializing_concurrently(tmp_path, monkeypatch, attempt):
    path = tmp_path / f"admit{attempt}.db"
    successes, errors = run_concurrent_fresh_initialization(path, monkeypatch)
    assert successes + errors == 2


# --- F6 / F7. WAL refusal and connection closure on every init failure ------


class _RefuseWal:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args):
        if " ".join(sql.split()).upper() == "PRAGMA JOURNAL_MODE=WAL":
            return self._real.execute("SELECT 'delete'")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_f6_wal_switch_refused_fails_closed(tmp_path, monkeypatch):
    rec = _ConnectRecorder(sqlite3, wrap=_RefuseWal)
    monkeypatch.setattr(gate_module, "sqlite3", rec)
    path = tmp_path / "admit.db"
    with pytest.raises(AdmissionError, match="not 'wal'"):
        _construct(path)
    assert len(rec.opened) == 1
    assert _all_closed(rec.opened)
    assert _journal_mode(path) == "delete"


def test_f7_i1_failure_closes_every_connection(tmp_path, recorder):
    path = tmp_path / "admit.db"
    _build(path, [V1_TABLE, V1_ROW])
    with pytest.raises(AdmissionError):
        _construct(path)
    assert recorder.opened and _all_closed(recorder.opened)


def test_f7_i3_failure_closes_every_connection(tmp_path, monkeypatch, recorder):
    path = tmp_path / "admit.db"
    _build(path, _v2())
    _hook(monkeypatch, lambda _g: _build(path, ["CREATE TABLE late (x)"]))
    with pytest.raises(AdmissionError, match="^migration required"):
        _construct(path)
    assert len(recorder.opened) == 2  # preflight (ro) + locked init (rw)
    assert _all_closed(recorder.opened)


def test_f7_i4_failure_closes_every_connection(tmp_path, monkeypatch):
    rec = _ConnectRecorder(sqlite3, wrap=_RefuseWal)
    monkeypatch.setattr(gate_module, "sqlite3", rec)
    path = tmp_path / "admit.db"
    _build(path, _v2())
    with pytest.raises(AdmissionError):
        _construct(path)
    assert len(rec.opened) == 2
    assert _all_closed(rec.opened)


# --- F8. documented limitation ---------------------------------------------


def test_f8_limitation_post_construction_schema_change_not_detected(tmp_path):
    """Documents a KNOWN LIMITATION (D10), not a guarantee.

    Schema validation runs at construction only. A structural change made by an
    outside writer after construction is not detected by reserve().
    """
    path = tmp_path / "admit.db"
    gate = _construct(path)
    _build(path, ["CREATE INDEX added_later ON reservations(sequence)"], journal_mode="wal")
    result = gate.reserve(_msg("assignment"), _now())
    assert result.outcome == "reserved"
