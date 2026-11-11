# SQLite Connection-Lifecycle Correction

Correction checkpoint for the BRIDGE-02A.2 offline adapter and the BRIDGE-02B.2.1
offline admission gate. Separate from BRIDGE-02B.2.2.

Base: `33f20da830ef481c35d3188167a70bd4ab750cb2`.
Branch: `feature/bridge-sqlite-connection-cleanup`.

## Problem
`with sqlite3.connect(...) as connection:` commits or rolls back, but it does not close the
connection. In CPython 3.12 the connection is also reachable from its own statement cache,
so it is not freed when the function returns. It stays open until a cyclic garbage
collection runs. On Windows an open SQLite connection prevents deleting the database file.
That made `test_reconciliation_detects_missing_receipt` and
`test_reconciliation_blocks_subsequent_admission` fail with `WinError 32`, at baseline
`ee74b49` as well as at `33f20da`. Linux CI did not see it because Linux allows deleting
open files.

## Correction
Each connection these components own is opened as
`with closing(<connect>) as connection, connection:`.

- The inner `connection` context manager keeps the existing commit-on-success and
  rollback-on-exception behavior.
- The outer `contextlib.closing` closes the connection after the transaction context
  exits, on success and on every exception path.
- Explicit `BEGIN IMMEDIATE`, `commit()`, and `rollback()` calls and their ordering are
  unchanged.

Sites changed:
- `offline_authenticated_adapter.py`: the eight adapter-owned `sqlite3.connect(...)` sites
  (`_schema_preflight`, `_reconcile`, `_init_receipts`, `_seen_ids`, `_expected_sequence`,
  `_advance_sequence`, `_record_receipt`, `accept`) and the two connections the adapter
  obtains through `ledger._connect()` (`_reconcile`, and the revision lookup in `accept`).
- `offline_admission_gate.py`: `_block_if_unresolved()`. `_init_db()` and `reserve()`
  already closed their connections and are unchanged.

## Not changed
- `checkpoint_ledger.py` and `offline_signature_verifier.py` are unchanged.
- The ledger's own internal connections (for example inside `record_event`) are owned by
  the ledger and are outside this correction. They remain a known limitation.
- No authorization, SHA, sequence, schema, or HALT behavior changes.
- `execution_enabled` remains False. External transport remains disabled.

## Regression coverage
`tests/test_sqlite_connection_lifecycle.py` tracks connections by owner:
- The `sqlite3` name inside each component module is replaced with a delegating tracker,
  so only that module's `connect` calls are recorded.
- `ledger._connect()` results are recorded only when the immediate caller is the adapter
  module, so ledger-internal connections are excluded.
- The tracker holds strong references, and closure is checked directly. The tests do not
  use garbage collection, retries, or delays.

Covered paths: construction, success, duplicate, rejection (identity conflict, HALT,
untrusted fixture), unresolved-state exceptions on construction and on admission, an
injected storage failure, and the adapter partial-write path. The two reconciliation tests
above remain as Windows file-deletion regressions.
