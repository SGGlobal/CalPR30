# BRIDGE-02A — Offline Integrity Contract

Offline, synthetic, non-executing integrity contract for SKIPPY↔Jason development handoff.

**This is an offline integrity contract, not authenticated actor messaging.**
`payload_digest` is an integrity checksum over the payload. Trusted fixture injection is provided through a separate controlled dependency, not a claim asserted by inbound envelope data. No live secrets.

## Envelope fields
- project_id, task_id, checkpoint_id, assignment_id
- message_id, idempotency_key
- sender, recipient, role
- payload, payload_digest (SHA-256 integrity checksum)
- schema_version, timestamp, expiry, sequence
- reviewed_sha (optional, format-validated)

## Authorization matrix
Exact (sender, role, recipient, type) combinations are required. Unknown combinations are rejected. Assignment requires a trusted fixture dependency injected by the adapter.

## Fail-closed rules
- Unauthorized sender/role/recipient/type rejected.
- Payload digest mismatch rejected.
- Replay of message_id or idempotency_key rejected (durable, survives restart).
- Sequence is enforced durably per stream; gaps and reorder are rejected.
- Expired or future-dated envelopes rejected.
- External transport is disabled (`TRANSPORT_ENABLED = False`).
- HALT is derived from authoritative state, not a caller flag.
- No automatic retry or catch-up after HALT.

## SHA evidence preservation
Implementation and review payloads preserve `sha`. Review payloads preserve `source_verified`. The ledger projection reflects the actual implementation SHA and source-verified status.

## Partial-write policy
Ledger rejections produce an UNCERTAIN receipt. If the ledger succeeds but the receipt write fails, the outcome is reported as a partial-write error. No cross-database atomicity is claimed. SQLite UPDATE/DELETE triggers are application constraints, not tamper-evident audit security.

## Security notes
- Synthetic fixtures only. No credentials. No network.
- execution_enabled remains False.
- Does not grant PR or merge authorization.
- Does not claim cross-database atomicity.
- Independent of the pending identity verification halt enforcement branch.

See `skippy/collaboration/authenticated_envelope.py`, `offline_authenticated_adapter.py`, and `tests/test_authenticated_envelope.py`.

## BRIDGE-02A.3 corrections
- Authoritative HALT check is required at construction. Omitted or falsey halt_check is rejected. Fail-closed on HALT.
- Persistent fail-stop state is recorded on partial writes. Subsequent calls and restarts are blocked until manual reconciliation. No automatic continuation.
- Sequence and receipt operations use BEGIN IMMEDIATE for cross-instance serialization. Concurrent adapter instances share the same admission gate.
- Malformed payloads produce controlled EnvelopeError and a rejection receipt when safe.
- Implementation, correction, and review SHA values are validated for format. source_verified must be literal True. Objective and findings must be non-empty strings.
- Assignment identity is persisted and enforced across subsequent messages. Mismatched assignment IDs are rejected.
- trusted_fixture remains a test-only harness dependency. It is a commissioning blocker for production use and is never treated as verified Chief authorization.

execution_enabled remains False. External transport remains disabled.

## BRIDGE-02A.4 transaction consistency
- Admission uses a single exclusive SQLite transaction (BEGIN IMMEDIATE) spanning fail-stop check, HALT check, validation, receipt, sequence advance, and assignment identity. Related state changes commit atomically.
- Fail-stop state is durable and checked on every admission, including after restart. Subsequent calls are blocked until manual reconciliation.
- Concurrent adapter instances sharing the same receipts database are serialized by the exclusive transaction. Conflicting sequence admission is rejected.
- HALT is derived from the required authoritative callback. Missing or failed HALT checks are fail-closed.
- Review source_verified remains synthetic fixture evidence only. It is a commissioning blocker and is never treated as independently verified authorization.

execution_enabled remains False. External transport remains disabled. No cross-database atomicity is claimed.

## BRIDGE-02A.5 rollback and concurrency
- Partial-write failures now roll back the transaction before recording fail-stop. An accepted receipt is never persisted when a later step fails.
- Fail-stop remains durable and is checked on every admission, including after restart.
- Simultaneous multi-thread tests verify that independent adapter instances sharing the same database produce exactly one acceptance.
- Dead duplicate implementation code after the successful return path has been removed.
- HALT remains derived from the required authoritative callback. Review source_verified remains synthetic fixture evidence only.

execution_enabled remains False. External transport remains disabled. No cross-database atomicity is claimed.

## BRIDGE-02A.6 ledger↔receipt reconciliation
- Deterministic reconciliation runs on startup and before every admission.
- Ledger events are compared against accepted receipts. Any mismatch, unreadable store, or missing receipt database with a nonempty ledger fails closed and requires manual reconciliation.
- The adapter never silently recreates a clean state over existing ledger evidence.
- Fail-stop remains effective. If recording fail-stop itself fails, the next admission still reconciles against the ledger and refuses.
- Fault-injection tests cover missing receipt database after a successful ledger commit, and restart with divergent state.

### Commissioning blockers (unchanged)
- payload_digest is an integrity checksum, not actor authentication.
- trusted_fixture is a test-only harness.
- Review source_verified is synthetic fixture evidence only.
- HALT callback is a synthetic check, not proven authoritative global HALT wiring.
- No cross-database atomicity is claimed.

execution_enabled remains False. External transport remains disabled.

## BRIDGE-02A.7 adapter-owned intent journal
- Durable PENDING intent is recorded before any ledger write.
- Explicit states: PENDING, ACCEPTED, UNCERTAIN.
- PENDING is always ambiguous on restart and fails closed. No automatic retry, acceptance, or recovery.
- Reconciliation is scoped to ledger events carrying `adapter_origin: bridge-02a`. Independent BRIDGE-01 events are ignored.
- ACCEPTED intents must match adapter-originated ledger events by message ID. Reverse mismatches fail closed.
- Concurrent instances are serialized by the exclusive journal transaction. Any unresolved PENDING blocks new admissions.
- Protected BRIDGE-01 ledger semantics are unchanged. The origin marker is carried in the existing event payload.

### Manual reconciliation runbook
1. Inspect the intent journal for PENDING or UNCERTAIN rows.
2. Compare against adapter-originated ledger events (`payload.adapter_origin == "bridge-02a"`).
3. If a PENDING intent has no matching ledger event, the ledger write did not occur. The intent remains blocked until Chief-authorized manual resolution.
4. If a ledger event exists without an ACCEPTED intent, the journal commit failed after the ledger commit. Manual reconciliation is required.
5. Never automatically clear HALT, retry, or mark recovered. Chief authorization is required for any correction.

### Commissioning blockers (unchanged)
- payload_digest is an integrity checksum, not actor authentication.
- trusted_fixture is a test-only harness.
- Review source_verified is synthetic fixture evidence only.
- HALT callback is a synthetic check, not proven authoritative global HALT wiring.
- No cross-database atomicity is claimed.

execution_enabled remains False. External transport remains disabled.

## BRIDGE-02A.8 intent-journal safety
- Admission-time durable single-active-intent gate under the exclusive SQLite transaction. Any PENDING or UNCERTAIN intent blocks new admission.
- UNCERTAIN is included in reconciliation. Any unresolved state blocks subsequent admission and restart.
- message_id and idempotency_key are UNIQUE in the intent journal. State transitions check affected row count.
- Concurrent instances cannot bypass an active PENDING operation.
- Protected BRIDGE-01 ledger semantics remain unchanged.

execution_enabled remains False. External transport remains disabled.

## BRIDGE-02A.9 offline integrity closure
- Bidirectional exact identity reconciliation compares message ID, idempotency key, project, task, checkpoint, assignment, and payload digest between the journal and adapter-originated ledger events. Fail closed on mismatch.
- Every ACCEPTED journal entry must have a corresponding accepted audit receipt. Sequence state is reconstructed from accepted journal history per stream and compared against sequence_state. Fail closed on gap or mismatch.
- Schema preflight inspects actual SQLite indices. An existing intent_journal without UNIQUE constraints fails closed with a migration-required message. No automatic mutation of protected state.
- Deterministic tests cover identity mismatch, missing accepted receipt, and legacy schema without uniqueness.

### Acceptance matrix status
1. Matching message ID, conflicting idempotency key — PASS (identity comparison).
2. Matching message ID, conflicting assignment identity — PASS (identity comparison).
3. Matching message ID, conflicting payload digest — PASS (identity comparison).
4. Missing accepted receipt — PASS (receipt verification).
5. Corrupted sequence state — PASS (sequence reconstruction).
6. Existing journal without UNIQUE constraints — PASS (schema preflight).
7. Concurrent admissions during PENDING-to-ledger window — PASS (admission-time gate; live-window test present).

execution_enabled remains False. External transport remains disabled.
