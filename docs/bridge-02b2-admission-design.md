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

## Remaining limitations
- No operational Chief authorization or SKIPPY review. Evidence is synthetic.
- HALT is an injected probe, not authoritative global HALT. A change between probe and commit is an uncovered race.
- No cross-database atomicity with the protected ledger. This slice does not write that ledger.
- SQLite constraints are not tamper-evident audit security.
- Process-local registry rollback protection from BRIDGE-02B.1 is not a durable trust anchor.

execution_enabled remains False. External transport remains disabled.
