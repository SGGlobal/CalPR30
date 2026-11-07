"""
SKIPPY AI Collaboration Bridge — Isolated Coordinated HALT tests.

Synthetic. Offline. Non-executing.
The fixture below is test-only. It is not a production bootstrap API.
"""

from __future__ import annotations

import sqlite3
from multiprocessing import Process, Queue
from pathlib import Path

import pytest

from skippy.collaboration.coordinated_halt import (
    SUPPORTED_SCHEMA_VERSION,
    CoordinatedHalt,
    HaltError,
    HaltStatus,
)


def _init_authority(path: Path, state: str = "NOT_HALTED", sequence: int = 0) -> None:
    """
    Test-only fixture. Creates a synthetic initialized authority database.
    Not a production initialization or recovery API.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE halt_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                state TEXT NOT NULL CHECK (state IN ('NOT_HALTED', 'HALTED')),
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE halt_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                sequence INTEGER,
                detail TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (SUPPORTED_SCHEMA_VERSION,),
        )
        connection.execute(
            """
            INSERT INTO halt_state (singleton, state, sequence, updated_at, reason)
            VALUES (1, ?, ?, '2026-01-01T00:00:00+00:00', 'test-init')
            """,
            (state, sequence),
        )


def _process_activate(path: str, queue: Queue) -> None:
    try:
        halt = CoordinatedHalt(path)
        sequence = halt.activate_halt("process")
        queue.put(("ok", sequence))
    except Exception as exc:  # noqa: BLE001 — test boundary
        queue.put(("err", str(exc)))


def test_valid_initialized_not_halted(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    halt = CoordinatedHalt(database)
    status, sequence = halt.read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0
    halt.require_not_halted()


def test_halt_activation_and_sequence(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    halt = CoordinatedHalt(database)
    sequence = halt.activate_halt("chief halt")
    assert sequence == 1
    status, read_sequence = halt.read_status()
    assert status is HaltStatus.HALTED
    assert read_sequence == 1
    with pytest.raises(HaltError):
        halt.require_not_halted()


def test_duplicate_halt_does_not_increment(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    halt = CoordinatedHalt(database)
    first = halt.activate_halt("first")
    second = halt.activate_halt("second")
    assert first == second == 1
    _status, sequence = halt.read_status()
    assert sequence == 1


def test_persistence_across_reopen(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    CoordinatedHalt(database).activate_halt("persist")
    reopened = CoordinatedHalt(database)
    status, sequence = reopened.read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1


def test_multiple_independent_instances(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    first = CoordinatedHalt(database)
    second = CoordinatedHalt(database)
    first.activate_halt("shared")
    status, sequence = second.read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1


def test_concurrent_thread_activation(tmp_path):
    import threading

    database = tmp_path / "halt.db"
    _init_authority(database)
    barrier = threading.Barrier(8)
    results: list[int] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()
            results.append(CoordinatedHalt(database).activate_halt("thread"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert not errors
    assert results
    assert set(results) == {1}
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1


def test_concurrent_process_activation(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    queue: Queue = Queue()
    processes = [
        Process(target=_process_activate, args=(str(database), queue))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    outcomes = [queue.get(timeout=5) for _ in processes]
    assert all(kind == "ok" for kind, _value in outcomes)
    sequences = {value for _kind, value in outcomes}
    assert sequences == {1}
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1


def test_missing_database_is_unknown_and_not_created(tmp_path):
    database = tmp_path / "missing.db"
    halt = CoordinatedHalt(database)
    status, sequence = halt.read_status()
    assert status is HaltStatus.UNKNOWN
    assert sequence is None
    with pytest.raises(HaltError):
        halt.require_not_halted()
    with pytest.raises(HaltError):
        halt.activate_halt("nope")
    assert not database.exists()


def test_corrupted_database(tmp_path):
    database = tmp_path / "corrupt.db"
    database.write_bytes(b"this is not a sqlite database")
    halt = CoordinatedHalt(database)
    status, sequence = halt.read_status()
    assert status is HaltStatus.UNKNOWN
    assert sequence is None
    with pytest.raises(HaltError):
        halt.require_not_halted()


def test_missing_control_row(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM halt_state")
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.UNKNOWN
    assert sequence is None


def test_unknown_control_state(tmp_path):
    database = tmp_path / "halt.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE halt_state (
                singleton INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        connection.execute(
            """
            INSERT INTO halt_state (singleton, state, sequence, updated_at, reason)
            VALUES (1, 'MAYBE', 0, '2026-01-01T00:00:00+00:00', 'bad')
            """
        )
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.UNKNOWN
    assert sequence is None


def test_unsupported_schema_version(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'"
        )
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.UNKNOWN
    assert sequence is None
    with pytest.raises(HaltError):
        CoordinatedHalt(database).activate_halt("nope")


def test_invalid_sequence(tmp_path):
    database = tmp_path / "halt.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE halt_state (
                singleton INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        connection.execute(
            """
            INSERT INTO halt_state (singleton, state, sequence, updated_at, reason)
            VALUES (1, 'NOT_HALTED', -1, '2026-01-01T00:00:00+00:00', 'bad')
            """
        )
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.UNKNOWN
    assert sequence is None


def test_database_lock_timeout(tmp_path, monkeypatch):
    database = tmp_path / "halt.db"
    _init_authority(database)
    holder = sqlite3.connect(database, timeout=5.0)
    holder.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(
        "skippy.collaboration.coordinated_halt.LOCK_TIMEOUT_SECONDS", 0.2
    )
    halt = CoordinatedHalt(database)
    with pytest.raises(HaltError):
        halt.activate_halt("blocked")
    holder.rollback()
    holder.close()
    status, _sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.NOT_HALTED


def test_failed_mandatory_transaction(tmp_path, monkeypatch):
    database = tmp_path / "halt.db"
    _init_authority(database)

    real_connect = sqlite3.connect

    def failing_open(self, readonly=False):
        connection = real_connect(
            self.database_path.resolve().as_uri()
            + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True,
            timeout=5.0,
        )
        original_execute = connection.execute

        class Proxy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **kw):
                if isinstance(sql, str) and "UPDATE halt_state" in sql:
                    raise sqlite3.OperationalError("forced mandatory failure")
                return original_execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        return Proxy(connection)

    monkeypatch.setattr(CoordinatedHalt, "_connect", failing_open)
    with pytest.raises(HaltError):
        CoordinatedHalt(database).activate_halt("operator")
    monkeypatch.undo()
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_connection_failure_raises_halt_error(tmp_path, monkeypatch):
    database = tmp_path / "halt.db"
    _init_authority(database)

    def fail_connect(self, readonly=False):
        raise sqlite3.OperationalError("cannot open")

    monkeypatch.setattr(CoordinatedHalt, "_connect", fail_connect)
    with pytest.raises(HaltError):
        CoordinatedHalt(database).activate_halt("operator")
    monkeypatch.undo()
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_configuration_failure_raises_halt_error(tmp_path, monkeypatch):
    database = tmp_path / "halt.db"
    _init_authority(database)

    real_connect = sqlite3.connect

    def fail_pragma(self, readonly=False):
        connection = real_connect(
            self.database_path.resolve().as_uri() + "?mode=rw",
            uri=True,
            timeout=5.0,
        )
        original_execute = connection.execute

        class Proxy:
            def execute(self, sql, *a, **kw):
                if isinstance(sql, str) and sql.startswith("PRAGMA"):
                    raise sqlite3.OperationalError("pragma failed")
                return original_execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(connection, name)

        proxy = Proxy()
        proxy.execute("PRAGMA synchronous=FULL")
        return proxy

    monkeypatch.setattr(CoordinatedHalt, "_connect", fail_pragma)
    with pytest.raises(HaltError):
        CoordinatedHalt(database).activate_halt("operator")
    monkeypatch.undo()
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_audit_failure_does_not_undo_halt(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE halt_events")
    halt = CoordinatedHalt(database)
    sequence = halt.activate_halt("audit-fail")
    assert sequence == 1
    status, read_sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.HALTED
    assert read_sequence == 1


def test_no_automatic_database_recreation(tmp_path):
    database = tmp_path / "absent.db"
    halt = CoordinatedHalt(database)
    halt.read_status()
    with pytest.raises(HaltError):
        halt.activate_halt("create?")
    assert not database.exists()


def test_no_public_halt_reset_api():
    public = {
        name
        for name in dir(CoordinatedHalt)
        if not name.startswith("_")
    }
    assert "clear" not in public
    assert "reset" not in public
    assert "resume" not in public
    assert "clear_halt" not in public


def test_no_protected_execution_capability(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    halt = CoordinatedHalt(database)
    assert halt.execution_enabled is False
    assert CoordinatedHalt.execution_enabled is False
    halt.activate_halt("still-disabled")
    assert halt.execution_enabled is False


def test_database_path_handling(tmp_path):
    database = tmp_path / "nested" / "halt.db"
    _init_authority(database)
    halt = CoordinatedHalt(str(database))
    status, sequence = halt.read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0
    other = CoordinatedHalt(Path(database))
    other.activate_halt("path")
    status, sequence = halt.read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1


def test_reason_codes_do_not_persist_credentials(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    secret = "password=hunter2 api_key=sk-test signature=abcdef"
    CoordinatedHalt(database).activate_halt(secret)
    with sqlite3.connect(database) as connection:
        reason = connection.execute(
            "SELECT reason FROM halt_state WHERE singleton = 1"
        ).fetchone()[0]
        details = [
            row[0]
            for row in connection.execute("SELECT detail FROM halt_events")
        ]
    assert reason == "unspecified"
    assert secret not in reason
    assert all(secret not in detail for detail in details)
    assert "hunter2" not in reason
    assert "sk-test" not in " ".join(details)


def test_allowed_reason_code_is_persisted(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database)
    CoordinatedHalt(database).activate_halt("operator")
    with sqlite3.connect(database) as connection:
        reason = connection.execute(
            "SELECT reason FROM halt_state WHERE singleton = 1"
        ).fetchone()[0]
    assert reason == "operator"


def test_not_halted_with_positive_sequence_is_unknown(tmp_path):
    database = tmp_path / "halt.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE halt_state (
                singleton INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        connection.execute(
            """
            INSERT INTO halt_state (singleton, state, sequence, updated_at, reason)
            VALUES (1, 'NOT_HALTED', 3, '2026-01-01T00:00:00+00:00', 'bad')
            """
        )
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.UNKNOWN
    assert sequence is None
    with pytest.raises(HaltError):
        CoordinatedHalt(database).activate_halt("operator")


def test_halted_with_zero_sequence_is_unknown(tmp_path):
    database = tmp_path / "halt.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE halt_state (
                singleton INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        connection.execute(
            """
            INSERT INTO halt_state (singleton, state, sequence, updated_at, reason)
            VALUES (1, 'HALTED', 0, '2026-01-01T00:00:00+00:00', 'bad')
            """
        )
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.UNKNOWN
    assert sequence is None


def test_existing_halted_persistence(tmp_path):
    database = tmp_path / "halt.db"
    _init_authority(database, state="HALTED", sequence=4)
    halt = CoordinatedHalt(database)
    status, sequence = halt.read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 4
    again = halt.activate_halt("operator")
    assert again == 4
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 4


def test_filesystem_error_during_activate_halt_becomes_halterror(tmp_path, monkeypatch):
    """Unexpected filesystem errors during HALT activation become HaltError."""
    database = tmp_path / "halt.db"
    _init_authority(database)
    halt = CoordinatedHalt(database)

    def boom_is_file(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "is_file", boom_is_file)

    with pytest.raises(HaltError, match="HALT activation failed"):
        halt.activate_halt("operator")

    # Authority remains NOT_HALTED (no partial activation).
    # Re-open without the patch.
    monkeypatch.undo()
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_sqlite_configuration_failure_closes_connection(tmp_path, monkeypatch):
    """
    When SQLite configuration (PRAGMA) fails inside _connect,
    the connection is closed before the error propagates.
    """
    database = tmp_path / "halt.db"
    _init_authority(database)

    closed = []
    real_connect = sqlite3.connect

    class TrackingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.startswith("PRAGMA"):
                raise sqlite3.OperationalError("injected pragma failure")
            return self._inner.execute(sql, *args, **kwargs)

        def close(self):
            closed.append(True)
            return self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def tracking_connect(*args, **kwargs):
        return TrackingConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(
        "skippy.collaboration.coordinated_halt.sqlite3.connect",
        tracking_connect,
    )

    halt = CoordinatedHalt(database)
    with pytest.raises(HaltError):
        halt.activate_halt("operator")

    assert closed, "connection was not closed after configuration failure"

    monkeypatch.undo()
    status, sequence = CoordinatedHalt(database).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0
