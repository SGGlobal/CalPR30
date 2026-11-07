"""
SKIPPY AI Collaboration Bridge — Legacy HALT migration safeguards tests.

Synthetic. Offline. Non-executing.
The coordinated authority fixture is test-only. It is not a production bootstrap.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from skippy.collaboration.authorization import AuthorizationPolicy
from skippy.collaboration.coordinated_halt import (
    SUPPORTED_SCHEMA_VERSION,
    CoordinatedHalt,
    HaltStatus,
)
from skippy.collaboration.legacy_halt_migration import (
    AbsentLegacyPolicy,
    LegacyHaltMigration,
    MigrationError,
    MigrationResult,
    OmittedSourcePolicy,
)
from skippy.collaboration.protocol import CollaborationProtocol


def _init_authority(path: Path, state: str = "NOT_HALTED", sequence: int = 0) -> None:
    """Test-only fixture. Not a production initialization API."""
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


def _legacy_policy(path: Path, halted: bool = False) -> AuthorizationPolicy:
    policy = AuthorizationPolicy(path)
    if halted:
        policy.halt()
    return policy


def _legacy_protocol(path: Path, halted: bool = False) -> CollaborationProtocol:
    protocol = CollaborationProtocol(path)
    if halted:
        protocol.halt()
    return protocol


def _migrate(
    coordinated: Path,
    policy: Path | None,
    protocol: Path | None,
    absent: AbsentLegacyPolicy = AbsentLegacyPolicy.ABSENT_LEGACY_IS_UNSAFE,
    omitted: OmittedSourcePolicy | None = None,
):
    # Existing scenarios that pass None opt in. Dedicated tests cover the
    # default fail-closed omission policy by constructing the helper directly.
    if omitted is None:
        omitted = (
            OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED
            if policy is None or protocol is None
            else OmittedSourcePolicy.OMITTED_SOURCE_IS_UNSAFE
        )
    helper = LegacyHaltMigration(
        coordinated,
        policy,
        protocol,
        absent_legacy_policy=absent,
        omitted_source_policy=omitted,
    )
    return helper.migrate()


def test_normal_migration_neither_legacy_halted(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=False)
    _legacy_protocol(protocol_db, halted=False)

    result = _migrate(coordinated, policy_db, protocol_db)

    assert result.execution_enabled is False
    assert result.activation_attempted is False
    assert result.authorization_policy_halted is False
    assert result.collaboration_protocol_halted is False
    assert result.coordinated_status == HaltStatus.NOT_HALTED.value
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_policy_halt_is_preserved_and_coordinated_activated(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)
    _legacy_protocol(protocol_db, halted=False)

    result = _migrate(coordinated, policy_db, protocol_db)

    assert result.execution_enabled is False
    assert result.activation_attempted is True
    assert result.authorization_policy_halted is True
    assert result.collaboration_protocol_halted is False
    assert result.coordinated_status == HaltStatus.HALTED.value

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1

    # Legacy record is preserved, never cleared.
    assert AuthorizationPolicy(policy_db).is_halted() is True
    with sqlite3.connect(policy_db) as connection:
        row = connection.execute(
            "SELECT value FROM authorization_control WHERE key = 'halted'"
        ).fetchone()
    assert row[0] == "1"


def test_protocol_halt_is_preserved_and_coordinated_activated(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=False)
    _legacy_protocol(protocol_db, halted=True)

    result = _migrate(coordinated, policy_db, protocol_db)

    assert result.activation_attempted is True
    assert result.collaboration_protocol_halted is True
    assert result.coordinated_status == HaltStatus.HALTED.value
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1
    assert CollaborationProtocol(protocol_db).is_halted is True


def test_both_legacy_halted_activates_once(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)
    _legacy_protocol(protocol_db, halted=True)

    result = _migrate(coordinated, policy_db, protocol_db)

    assert result.activation_attempted is True
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1
    assert AuthorizationPolicy(policy_db).is_halted() is True
    assert CollaborationProtocol(protocol_db).is_halted is True


def test_already_coordinated_halted_is_idempotent(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    _init_authority(coordinated, state="HALTED", sequence=3)
    _legacy_policy(policy_db, halted=True)

    result = _migrate(coordinated, policy_db, None, AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE)

    assert result.activation_attempted is True
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
    # Idempotent: already HALTED, sequence is not incremented.
    assert sequence == 3


def test_repeated_migration_is_idempotent(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)
    _legacy_protocol(protocol_db, halted=False)

    first = _migrate(coordinated, policy_db, protocol_db)
    second = _migrate(coordinated, policy_db, protocol_db)

    assert first.execution_enabled is False
    assert second.execution_enabled is False
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1
    assert AuthorizationPolicy(policy_db).is_halted() is True


def test_absent_legacy_is_unsafe_by_default(tmp_path):
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    missing = tmp_path / "missing-policy.db"

    with pytest.raises(MigrationError, match="absent"):
        _migrate(coordinated, missing, None)

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0
    assert not missing.exists()


def test_absent_legacy_explicitly_safe(tmp_path):
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    missing = tmp_path / "missing-policy.db"

    result = _migrate(
        coordinated,
        missing,
        None,
        AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE,
    )

    assert result.execution_enabled is False
    assert result.authorization_policy_halted is False
    assert result.collaboration_protocol_halted is None
    assert result.activation_attempted is False
    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED


def test_missing_coordinated_authority_is_not_created(tmp_path):
    coordinated = tmp_path / "missing-coord.db"
    policy_db = tmp_path / "policy.db"
    _legacy_policy(policy_db, halted=True)

    with pytest.raises(MigrationError, match="missing or unreadable"):
        _migrate(coordinated, policy_db, None, AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE)

    assert not coordinated.exists()
    assert AuthorizationPolicy(policy_db).is_halted() is True


def test_corrupted_legacy_database_fails_closed(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    _init_authority(coordinated)
    policy_db.write_bytes(b"this is not a sqlite database")

    with pytest.raises(MigrationError, match="unreadable"):
        _migrate(coordinated, policy_db, None, AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE)

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_incompatible_schema_fails_closed(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    _init_authority(coordinated)
    with sqlite3.connect(policy_db) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")

    with pytest.raises(MigrationError, match="incompatible"):
        _migrate(coordinated, policy_db, None, AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE)

    status, _sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED


def test_uncertain_halt_value_fails_closed(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=False)
    with sqlite3.connect(policy_db) as connection:
        connection.execute(
            "INSERT INTO authorization_control (key, value) VALUES ('halted', 'maybe')"
        )

    with pytest.raises(MigrationError, match="uncertain"):
        _migrate(coordinated, policy_db, None, AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE)

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_legacy_lock_failure_fails_closed(tmp_path, monkeypatch):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)

    holder = sqlite3.connect(policy_db, timeout=5.0)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("UPDATE authorization_control SET value = value WHERE key = 'halted'")

    monkeypatch.setattr(
        "skippy.collaboration.legacy_halt_migration.LEGACY_LOCK_TIMEOUT_SECONDS",
        0.2,
    )

    with pytest.raises(MigrationError, match="unreadable"):
        _migrate(coordinated, policy_db, None, AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE)

    holder.rollback()
    holder.close()

    # Failed closed: coordinated not activated, legacy still halted.
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0
    assert AuthorizationPolicy(policy_db).is_halted() is True


def test_partial_failure_does_not_activate(tmp_path):
    """One readable halted legacy and one corrupted legacy fails closed."""
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)
    protocol_db.write_bytes(b"corrupt")

    with pytest.raises(MigrationError):
        _migrate(coordinated, policy_db, protocol_db)

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0
    assert AuthorizationPolicy(policy_db).is_halted() is True


def test_concurrent_migration_attempts(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)
    _legacy_protocol(protocol_db, halted=False)

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        try:
            barrier.wait(timeout=2)
            results.append(_migrate(coordinated, policy_db, protocol_db))
        except Exception as exc:  # noqa: BLE001 — collect test failures
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    assert all(result.execution_enabled is False for result in results)
    assert all(result.coordinated_status == HaltStatus.HALTED.value for result in results)
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1
    assert AuthorizationPolicy(policy_db).is_halted() is True


def test_unreadable_coordinated_fails_closed(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    coordinated.write_bytes(b"not a database")
    _legacy_policy(policy_db, halted=True)

    with pytest.raises(MigrationError, match="missing or unreadable"):
        _migrate(coordinated, policy_db, None, AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE)

    assert AuthorizationPolicy(policy_db).is_halted() is True


def test_migration_reason_code_is_controlled(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)

    _migrate(coordinated, policy_db, None, AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE)

    with sqlite3.connect(coordinated) as connection:
        rows = connection.execute(
            "SELECT reason FROM halt_state"
        ).fetchall()
        events = connection.execute(
            "SELECT detail FROM halt_events"
        ).fetchall()

    assert rows[0][0] == "migration"
    # No free-form or credential-like text is stored.
    for (detail,) in events:
        assert detail in {"migration", "unspecified", "test", "operator", "policy", "protocol", "coordination"}
        assert "secret" not in detail
        assert "token" not in detail


def test_no_protected_execution_capability(tmp_path):
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)
    helper = LegacyHaltMigration(
        coordinated,
        None,
        None,
        absent_legacy_policy=AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE,
        omitted_source_policy=OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED,
    )
    result = helper.migrate()
    assert helper.execution_enabled is False
    assert result.execution_enabled is False
    assert not hasattr(helper, "submit_order")
    assert not hasattr(helper, "deploy")


def test_invalid_absent_policy_rejected(tmp_path):
    with pytest.raises(MigrationError, match="Invalid absent-legacy policy"):
        LegacyHaltMigration(
            tmp_path / "coord.db",
            None,
            None,
            absent_legacy_policy="whatever",  # type: ignore[arg-type]
            omitted_source_policy=OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED,
        )


def test_omitted_source_is_unsafe_by_default(tmp_path):
    """Omitted legacy paths cannot silently bypass fail-closed policy."""
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=False)

    with pytest.raises(MigrationError, match="omitted without explicit authorization"):
        LegacyHaltMigration(coordinated, policy_db, None).migrate()

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_omitted_source_explicitly_authorized(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)

    result = LegacyHaltMigration(
        coordinated,
        policy_db,
        None,
        omitted_source_policy=OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED,
    ).migrate()

    assert result.execution_enabled is False
    assert result.collaboration_protocol_halted is None
    assert result.authorization_policy_halted is True
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1
    assert AuthorizationPolicy(policy_db).is_halted() is True


def test_both_sources_omitted_requires_authorization(tmp_path):
    coordinated = tmp_path / "coord.db"
    _init_authority(coordinated)

    with pytest.raises(MigrationError, match="omitted without explicit authorization"):
        LegacyHaltMigration(coordinated, None, None).migrate()

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_directory_path_is_not_treated_as_absent(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_dir = tmp_path / "policy-dir"
    _init_authority(coordinated)
    policy_dir.mkdir()

    with pytest.raises(MigrationError, match="not a database file"):
        LegacyHaltMigration(
            coordinated,
            policy_dir,
            None,
            absent_legacy_policy=AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE,
            omitted_source_policy=OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED,
        ).migrate()

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_dangling_symlink_is_not_treated_as_absent(tmp_path):
    coordinated = tmp_path / "coord.db"
    dangling = tmp_path / "dangling.db"
    _init_authority(coordinated)
    dangling.symlink_to(tmp_path / "does-not-exist.db")

    with pytest.raises(MigrationError, match="not a database file"):
        LegacyHaltMigration(
            coordinated,
            dangling,
            None,
            absent_legacy_policy=AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE,
            omitted_source_policy=OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED,
        ).migrate()

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_genuinely_missing_file_still_honors_absence_policy(tmp_path):
    coordinated = tmp_path / "coord.db"
    missing = tmp_path / "missing.db"
    _init_authority(coordinated)

    result = LegacyHaltMigration(
        coordinated,
        missing,
        None,
        absent_legacy_policy=AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE,
        omitted_source_policy=OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED,
    ).migrate()

    assert result.authorization_policy_halted is False
    assert result.activation_attempted is False
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0


def test_partial_failure_preserves_legacy_halt_and_does_not_activate(tmp_path):
    """
    AuthorizationPolicy HALTED + CollaborationProtocol unreadable +
    CoordinatedHalt NOT_HALTED fails closed without activation.
    """
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=True)
    protocol_db.write_bytes(b"not a database")

    with pytest.raises(MigrationError, match="unreadable"):
        LegacyHaltMigration(coordinated, policy_db, protocol_db).migrate()

    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.NOT_HALTED
    assert sequence == 0
    assert AuthorizationPolicy(policy_db).is_halted() is True


def test_invalid_omitted_policy_rejected(tmp_path):
    with pytest.raises(MigrationError, match="Invalid omitted-source policy"):
        LegacyHaltMigration(
            tmp_path / "coord.db",
            None,
            None,
            omitted_source_policy="nope",  # type: ignore[arg-type]
        )


def test_migration_result_execution_disabled_cannot_be_overridden():
    """Callers cannot construct a MigrationResult with execution enabled."""
    result = MigrationResult(
        coordinated_status="NOT_HALTED",
        authorization_policy_halted=False,
        collaboration_protocol_halted=False,
        activation_attempted=False,
    )
    assert result.execution_enabled is False

    with pytest.raises(TypeError):
        MigrationResult(
            coordinated_status="NOT_HALTED",
            authorization_policy_halted=False,
            collaboration_protocol_halted=False,
            activation_attempted=False,
            execution_enabled=True,
        )

    with pytest.raises(Exception):
        result.execution_enabled = True
    assert result.execution_enabled is False


def test_normal_migration_result_always_execution_disabled(tmp_path):
    coordinated = tmp_path / "coord.db"
    policy_db = tmp_path / "policy.db"
    protocol_db = tmp_path / "protocol.db"
    _init_authority(coordinated)
    _legacy_policy(policy_db, halted=False)
    _legacy_protocol(protocol_db, halted=False)

    result = _migrate(coordinated, policy_db, protocol_db)
    assert isinstance(result, MigrationResult)
    assert result.execution_enabled is False
    assert result.activation_attempted is False
    assert result.coordinated_status == HaltStatus.NOT_HALTED.value

    # Existing HALT preservation and activation behavior remain intact.
    _legacy_policy(policy_db, halted=True)
    halted = _migrate(coordinated, policy_db, protocol_db)
    assert halted.execution_enabled is False
    assert halted.activation_attempted is True
    status, sequence = CoordinatedHalt(coordinated).read_status()
    assert status is HaltStatus.HALTED
    assert sequence == 1
    assert AuthorizationPolicy(policy_db).is_halted() is True
