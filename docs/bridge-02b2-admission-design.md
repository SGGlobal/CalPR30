# BRIDGE-02B.2.1 Offline Durable Admission Reservation

Synthetic-only SQLite reservation gate. It calls the BRIDGE-02B.1 verifier and records durable message identity. It does not write the protected BRIDGE-02A ledger.

## Behavior
- `BEGIN IMMEDIATE`, WAL, `synchronous=FULL`.
- Unique `(project_id, task_id, message_id)` and `(project_id, task_id, idempotency_key)`.
- Sequence starts at 1 and must be exact.
- Sender and assignment are bound to the stream.
- Injected evidence verifier. A caller-supplied verified flag is never accepted.
- Injected HALT probe. `halted` and `unavailable` reject.
- `PENDING` and `UNCERTAIN` block admission after restart. No automatic promotion.

## Security corrections (BRIDGE-02B.2.1 hardening)
- Closed authorization: only message types `assignment`, `implementation`, `correction`, and `review` are accepted. All others are rejected.
- Explicit evidence validation: missing, empty, or whitespace-only `evidence_id` is rejected before the evidence verifier is called. No hardcoded approval paths.
- Mandatory Chief authorization evidence for assignments (and for implementation/correction). Reviews require SKIPPY review evidence. Verification is fail-closed: any outcome other than `verified` (or any exception) rejects the reservation.
- Exhaustive identity matching for duplicates: all matching rows are examined. A duplicate is returned only when exactly one reservation exists and every required identity field matches exactly (including `RESERVED` state). Any conflicting, multiple, or ambiguous match is rejected.
- Concurrent reservation attempts use the same transaction isolation; identical concurrent reservations yield exactly one reserved and one duplicate; conflicting ones yield one reserved and one rejected. Neither bypasses identity checks.
- Storage failures (connection, query, transaction, commit) are fail-closed and raise `AdmissionError`. No successful admission is returned on storage error.

## Remaining limitations
- No operational Chief authorization or SKIPPY review. Evidence is synthetic.
- HALT is an injected probe, not authoritative global HALT. A change between probe and commit is an uncovered race.
- No cross-database atomicity with the protected ledger. This slice does not write that ledger.
- SQLite constraints are not tamper-evident audit security.
- Process-local registry rollback protection from BRIDGE-02B.1 is not a durable trust anchor.

execution_enabled remains False. External transport remains disabled.
