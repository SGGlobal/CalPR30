"""
SKIPPY AI Collaboration Bridge — Legacy HALT Migration Safeguards.

OFFLINE. SYNTHETIC. NON-EXECUTING. ISOLATED.

This helper reads existing AuthorizationPolicy and CollaborationProtocol
HALT flags with read-only SQLite connections and, if either component is
HALTED, ensures the CoordinatedHalt authority becomes or remains HALTED.

It is not integrated into live component startup. It does not perform a
production cutover. It never creates a missing coordinated authority.
It never clears or resets a legacy HALT record.

A missing or unreadable coordinated authority is never silently recreated
as NOT_HALTED. Legitimately absent legacy databases are distinguished from
inaccessible, invalid, or corrupted filesystem objects. Absence-is-safe
applies only to a path that does not exist. Directories, dangling links,
and other non-file objects are invalid and fail closed.

Omitted legacy sources are also unsafe by default. A migration cannot
report success when a required source was never examined. Intentionally
omitted sources require explicit OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED.

Partial failure semantics (isolated helper):
If one legacy source is HALTED and another is unreadable, migration raises
MigrationError and does not activate the coordinated authority. This is
fail-closed for the helper: success is never reported on incomplete
evidence. It is safe only while the helper is not the sole HALT authority.
Future integration must not treat a failed migration as NOT_HALTED; it must
keep consulting legacy flags, or treat MigrationError as deny-work, until a
complete successful migration is obtained. CoordinatedHalt activation
semantics are unchanged.

execution_enabled remains False. No method grants protected execution.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from skippy.collaboration.coordinated_halt import (
    CoordinatedHalt,
    HaltError,
    HaltStatus,
)

# Controlled reason code already allowed by CoordinatedHalt.
MIGRATION_REASON = "migration"

LEGACY_LOCK_TIMEOUT_SECONDS = 5.0

AUTHORIZATION_CONTROL_TABLE = "authorization_control"
PROTOCOL_CONTROL_TABLE = "control"
HALT_KEY = "halted"
HALT_VALUE = "1"


class AbsentLegacyPolicy(str, Enum):
    """
    Explicit policy for a legacy database path that does not exist.

    Absence is not assumed safe. The caller must opt in.
    Applies only to a genuinely missing path, never to a directory,
    dangling symlink, or other invalid filesystem object.
    """

    ABSENT_LEGACY_IS_UNSAFE = "ABSENT_LEGACY_IS_UNSAFE"
    ABSENT_LEGACY_IS_SAFE = "ABSENT_LEGACY_IS_SAFE"


class OmittedSourcePolicy(str, Enum):
    """
    Explicit policy for a legacy source that was not given a path.

    Omission is not assumed safe. A migration must not report success
    when a required source was never examined unless the caller opts in.
    """

    OMITTED_SOURCE_IS_UNSAFE = "OMITTED_SOURCE_IS_UNSAFE"
    OMITTED_SOURCE_IS_AUTHORIZED = "OMITTED_SOURCE_IS_AUTHORIZED"


class MigrationError(RuntimeError):
    """
    Raised when legacy HALT migration cannot be completed safely.

    Fail closed. Never indicates that execution is permitted.
    """


@dataclass(frozen=True)
class MigrationResult:
    """Non-sensitive outcome of a migration attempt. execution_enabled is always False."""

    coordinated_status: str
    authorization_policy_halted: bool | None
    collaboration_protocol_halted: bool | None
    activation_attempted: bool
    execution_enabled: bool = False


class LegacyHaltMigration:
    """
    Isolated offline migration helper.

    Does not own a production bootstrap path. Does not clear HALT.
    Does not enable execution.
    """

    execution_enabled = False

    def __init__(
        self,
        coordinated_path: str | Path,
        authorization_policy_path: str | Path | None,
        collaboration_protocol_path: str | Path | None,
        *,
        absent_legacy_policy: AbsentLegacyPolicy = AbsentLegacyPolicy.ABSENT_LEGACY_IS_UNSAFE,
        omitted_source_policy: OmittedSourcePolicy = OmittedSourcePolicy.OMITTED_SOURCE_IS_UNSAFE,
    ):
        self.coordinated_path = Path(coordinated_path)
        self.authorization_policy_path = (
            None if authorization_policy_path is None else Path(authorization_policy_path)
        )
        self.collaboration_protocol_path = (
            None
            if collaboration_protocol_path is None
            else Path(collaboration_protocol_path)
        )
        if not isinstance(absent_legacy_policy, AbsentLegacyPolicy):
            raise MigrationError("Invalid absent-legacy policy")
        if not isinstance(omitted_source_policy, OmittedSourcePolicy):
            raise MigrationError("Invalid omitted-source policy")
        self.absent_legacy_policy = absent_legacy_policy
        self.omitted_source_policy = omitted_source_policy

    def migrate(self) -> MigrationResult:
        """
        Read legacy HALT flags and ensure the coordinated authority is HALTED
        if either legacy component is HALTED.

        Idempotent. Safe to retry. Never modifies legacy databases.
        Never creates a missing coordinated authority.
        """

        # Require a usable coordinated authority before mutating anything.
        # A missing or unreadable authority must not be recreated as NOT_HALTED.
        coordinated = CoordinatedHalt(self.coordinated_path)
        status, _sequence = coordinated.read_status()
        if status is HaltStatus.UNKNOWN:
            raise MigrationError("Coordinated authority is missing or unreadable")

        policy_halted = self._read_legacy(
            self.authorization_policy_path,
            AUTHORIZATION_CONTROL_TABLE,
            "authorization policy",
        )
        protocol_halted = self._read_legacy(
            self.collaboration_protocol_path,
            PROTOCOL_CONTROL_TABLE,
            "collaboration protocol",
        )

        needs_halt = bool(policy_halted) or bool(protocol_halted)
        activation_attempted = False

        if needs_halt:
            activation_attempted = True
            try:
                # Uses the existing controlled reason-code allowlist.
                coordinated.activate_halt(MIGRATION_REASON)
            except HaltError as exc:
                raise MigrationError("Coordinated HALT activation failed") from exc

            status, _sequence = coordinated.read_status()
            if status is not HaltStatus.HALTED:
                raise MigrationError("Coordinated authority did not become HALTED")

        return MigrationResult(
            coordinated_status=status.value,
            authorization_policy_halted=policy_halted,
            collaboration_protocol_halted=protocol_halted,
            activation_attempted=activation_attempted,
        )

    def _classify_legacy_path(self, path: Path, label: str) -> str:
        """
        Distinguish a genuinely missing database from an invalid path.

        Returns "missing" only when the path does not exist and is not a
        dangling symlink. Directories, non-file objects, and dangling
        symlinks are invalid. Inaccessible paths raise MigrationError.
        """

        try:
            if path.is_symlink() and not path.exists():
                raise MigrationError(f"{label} path is not a database file")
            if not path.exists():
                return "missing"
            if not path.is_file():
                raise MigrationError(f"{label} path is not a database file")
            return "file"
        except MigrationError:
            raise
        except OSError as exc:
            raise MigrationError(f"{label} database is inaccessible") from exc

    def _read_legacy(
        self,
        path: Path | None,
        table: str,
        label: str,
    ) -> bool | None:
        """
        Read a legacy HALT flag with a read-only connection.

        Returns True if halted, False if not halted, None if the path was
        explicitly omitted. Raises MigrationError on any unsafe condition.
        """

        if path is None:
            if (
                self.omitted_source_policy
                is OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED
            ):
                return None
            raise MigrationError(
                f"{label} source was omitted without explicit authorization"
            )

        classification = self._classify_legacy_path(path, label)
        if classification == "missing":
            if self.absent_legacy_policy is AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE:
                return False
            raise MigrationError(f"{label} database is absent")

        connection = None
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=LEGACY_LOCK_TIMEOUT_SECONDS,
            )
            # Confirm the required control table exists. Missing table is
            # an incompatible schema, not "not halted".
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if row is None:
                raise MigrationError(f"{label} schema is incompatible")

            columns = {
                col[1]
                for col in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "key" not in columns or "value" not in columns:
                raise MigrationError(f"{label} schema is incompatible")

            halt_row = connection.execute(
                f"SELECT value FROM {table} WHERE key = ?",
                (HALT_KEY,),
            ).fetchone()

            if halt_row is None:
                # Initialized component with no halt record is not halted.
                return False

            value = halt_row[0]
            if value == HALT_VALUE:
                return True
            # Any other stored value is uncertain. Fail closed.
            raise MigrationError(f"{label} HALT record is uncertain")
        except MigrationError:
            raise
        except sqlite3.Error as exc:
            raise MigrationError(f"{label} database is unreadable") from exc
        except OSError as exc:
            raise MigrationError(f"{label} database is inaccessible") from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
