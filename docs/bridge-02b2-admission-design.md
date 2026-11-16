# BRIDGE-02B.2.1 Offline Durable Admission Reservation

Synthetic-only SQLite reservation gate. It calls the BRIDGE-02B.1 verifier and records durable message identity. It does not write the protected BRIDGE-02A ledger.

## Behavior
- `BEGIN IMMEDIATE`, WAL, `synchronous=FULL`.
- Unique `(project_id, task_id, message_id)` and `(project_id, task_id, idempotency_key)`.
- Sequence starts at 1 and must be exact within `(project_id, task_id, checkpoint_id)`.
- Each message's actor is bound individually: the signature binds `sender` to the key's `actor_id` and `role` to the key's `permitted_role`, and the closed matrix below allows only the exact `(sender, role, recipient, type)` tuple. There is no stream owner; one stream legitimately carries Chief, Jason and Skippy messages. `assignment_id` is bound to the last `RESERVED` row of the sequence scope.
- Injected evidence verifier. A caller-supplied verified flag is never accepted.
- Injected HALT probe. `halted` and `unavailable` reject. Unexpected probe exceptions fail closed.
- `PENDING` and `UNCERTAIN` block admission after restart. No automatic promotion.

## Security corrections (BRIDGE-02B.2.1 hardening)
- Closed authorization: only message types `assignment`, `implementation`, `correction`, and `review` are accepted. All others are rejected.
- Explicit evidence validation: missing, empty, or whitespace-only `evidence_id` is rejected before the evidence verifier is called. No hardcoded approval paths.
- Mandatory Chief authorization evidence for assignments (and for implementation/correction). Reviews require SKIPPY review evidence. Verification is fail-closed: any outcome other than `verified` (or any exception) rejects the reservation.
- Exhaustive identity matching for duplicates: all matching rows are examined. A duplicate is returned only when exactly one reservation exists and every required identity field matches exactly (including `RESERVED` state). Any conflicting, multiple, or ambiguous match is rejected.
- Concurrent reservation attempts use the same transaction isolation; identical concurrent reservations yield exactly one reserved and one duplicate; conflicting ones yield one reserved and one rejected. Neither bypasses identity checks.
- Split-key collisions across distinct existing reservations are rejected as ambiguous or conflicting.
- Storage failures (connection, query, transaction, commit, including during initialization) are fail-closed and raise `AdmissionError`. No successful admission is returned on storage error.
- Unexpected HALT-probe exceptions are fail-closed and reject admission.

## Admission-security correction (findings 2, 3 and 5)

### Authorization matrix
| sender | role | recipient | type | evidence kind | bound SHA |
|---|---|---|---|---|---|
| chief | chief | jason | assignment | chief_authorization | `base_sha` |
| jason | implementer | skippy | implementation | chief_authorization | `implementation_sha` |
| jason | implementer | skippy | correction | chief_authorization | `implementation_sha` |
| skippy | reviewer | jason | review | skippy_review | `implementation_sha` |

Any other tuple is rejected. Signature, matrix and verified evidence are three separate requirements.

### `reserve()` order
1. `_block_if_unresolved()` (unchanged). This is the first database access: open, read, close.
2. HALT probe (unchanged).
3. Payload snapshot. Only an exact `SignedEnvelope` is accepted. The payload is copied once: `_canonical_json(payload)` (the verifier's encoder, imported read-only), `json.loads`, `dataclasses.replace`. Any failure (out-of-profile value, recursion, size change during iteration, any other exception) is `malformed payload`.
4. `verify_signature` on the snapshot envelope. Failures are `invalid signature: …`.
5. `type` is a `str` in the allowed set, else `unauthorized message type`.
6. Matrix tuple, else `unauthorized sender/role/recipient/type combination`.
7. `evidence_id`: a `str`, non-empty, equal to its own `.strip()`. No coercion and no trimming. `missing or empty evidence_id` / `evidence_id has surrounding whitespace`.
8. SHA fields: a top-level `sha` key is `adapter sha alias not accepted`; the SHA field not bound for the type is `forbidden field: …`; the bound field must be a `str` matching `re.fullmatch(r"[0-9a-f]{40}")`, else `invalid base_sha` / `invalid implementation_sha`. Assignments bind `base_sha`; implementation, correction and review bind `implementation_sha`. Format validation does not prove that a commit exists.
9. Evidence verifier, called only with `str` scalars from the snapshot. `evidence …` on any outcome other than `verified`.
10. `BEGIN IMMEDIATE`, identity lookup, duplicate or conflict, sequence, assignment binding, insert, commit.

Steps 3 to 9 run after the step-1 database check and before any further connection or reservation write; a rejection there opens no additional connection and writes nothing. A message reaches the identity lookup only after passing steps 3 to 9 on its own, so `conflicting identity` only describes a fully authorized message that reuses a stored `message_id` or `idempotency_key` with different content.

Authenticity of the snapshot comes from the signature check on the copy, not from any round-trip comparison. Steps 5 to 10 and every persisted field read only the verified snapshot; the caller's envelope is not re-read and not written.

### Schema (user_version 2)
Two columns are added after `sender`: `recipient TEXT NOT NULL` and `role TEXT NOT NULL`. Both are persisted and are part of duplicate matching. The two unique indexes, `AUTOINCREMENT` and all other columns are unchanged. No verdict, workflow, type, SHA or `evidence_id` columns are added.

### Initialization order
- **I1 read-only preflight.** A missing or 0-byte file is fresh. Otherwise the file is opened with `mode=ro` and only `SELECT`/`PRAGMA` reads run. The store must have exactly the objects `reservations`, `sqlite_sequence`, `uq_message`, `uq_idem`; the exact table definition and `table_xinfo` columns; both indexes unique, `origin='c'`, non-partial, with exactly the expected key columns in order, ascending, `BINARY`, no expressions; `AUTOINCREMENT`; `user_version = 2`. Anything else, including every database created by the previous schema, fails closed with `migration required`. A read-only open failure raises `AdmissionError`; there is no read-write fallback. Rejected stores keep their main file byte-for-byte.
- **I2.** Writable connection without the WAL or synchronous PRAGMAs, then `BEGIN IMMEDIATE` in the database's current journal mode.
- **I3 recheck under the lock.** If I1 found a fresh store and the schema is still empty, the DDL runs, `user_version` is set to 2 and the transaction commits. Otherwise the full structural validation runs again; an incompatible store is rolled back and rejected with `migration required` (logical no-mutation, journal mode never switched; a transient rollback journal may appear, so no byte-for-byte claim), a compatible store's empty transaction is rolled back.
- **I4.** Outside any transaction: one attempt at `PRAGMA journal_mode=WAL`. If it raises (including `database is locked` or busy contention) or reports anything other than `wal`, construction fails closed with `AdmissionError` and the connection is closed. Then `PRAGMA synchronous=FULL`.
- **I5.** `_block_if_unresolved()` (unchanged).

Concurrent initialization of the same store is serialized by `BEGIN IMMEDIATE` but is not atomic: a concurrent initializer may fail closed with `AdmissionError` (for example when SQLite reports the database as locked during the WAL switch). The schema DDL is transactional, so such a failure leaves either no schema or the complete v2 schema, never a partial one. The caller may retry construction; the gate itself does not retry.

Every connection is closed explicitly on every path. Existing evidence is never altered, dropped, replaced or migrated automatically; migration is a manual step outside this slice.

## Remaining limitations
- No operational Chief authorization or SKIPPY review. Evidence and keys are synthetic.
- HALT is an injected probe, not authoritative global HALT. A change between probe and commit is an uncovered race (finding 6, unresolved).
- The locked schema validation (I3) and the WAL switch (I4) are not atomic together, because SQLite cannot change `journal_mode` inside a transaction. Another writer could change the schema between them, and the gate does not revalidate after I4. This gap is documented, not eliminated.
- Between I3 and I4 a fresh store is visible as a valid v2 database in rollback-journal mode.
- Concurrent initialization may fail closed with `AdmissionError`; retrying is the caller's decision. No atomicity is claimed for concurrent initialization.
- Schema validation runs at construction only. A schema change made by an outside writer after construction is not detected by `reserve()`.
- A hostile writer with access to the database file is out of scope.
- No cross-database atomicity with the protected ledger. This slice does not write that ledger.
- SQLite constraints are not tamper-evident audit security.
- Process-local registry rollback protection from BRIDGE-02B.1 is not a durable trust anchor.
- Deferred workflow policies (not enforced): assignment opening the stream, alternating transitions, review verdicts and stream closure, review SHA equal to the latest implementation SHA, unique correction SHAs, implementation SHA differing from `base_sha`, `correction_request` in the gate. Any matrix-valid message is accepted at the next sequence number.
- Adapter compatibility (mapping `sha` to `implementation_sha`, and adapter parity for sequence, assignment and SHA rules) is deferred to the paused BRIDGE-02B.2.2. The adapter is unchanged.

execution_enabled remains False. External transport remains disabled.
