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
