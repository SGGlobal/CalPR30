# BRIDGE-02A — Authenticated Envelope Contract (Offline)

Offline, synthetic, non-executing communication contract for SKIPPY↔Jason development handoff.

## Envelope fields
- project_id, task_id, checkpoint_id, assignment_id
- message_id, idempotency_key
- sender, recipient, role
- payload, payload_digest (SHA-256)
- schema_version, timestamp, expiry, sequence
- reviewed_sha (optional)
- chief_authorization (required for assignment dispatch)

## Fail-closed rules
- Unauthorized sender/role/recipient rejected.
- Payload digest mismatch rejected.
- Replay of message_id or idempotency_key rejected.
- Expired or future-dated envelopes rejected.
- Sequence mismatch rejected.
- Assignment dispatch requires explicit Chief authorization.
- External transport is disabled (`TRANSPORT_ENABLED = False`).
- No automatic retry or catch-up after HALT.

## Security notes
- Synthetic fixtures only. No credentials. No network.
- execution_enabled remains False.
- Does not grant PR or merge authorization.
- Does not claim cross-database atomicity.
- Independent of the pending identity verification halt enforcement branch.

See `skippy/collaboration/authenticated_envelope.py`, `offline_authenticated_adapter.py`, and `tests/test_authenticated_envelope.py`.

## BRIDGE-02A.1 security corrections
1. **Integrity checksum, not authentication.** payload_digest is documented as an integrity checksum. Trusted fixture injection is modeled separately. No live secrets.
2. **Authorization matrix.** Exact sender/role/payload-type mapping required. Role spoofing (implementer + assignment) is rejected. Unknown types are rejected.
3. **Ledger revision.** Derived from the ledger, never from message sequence. Nonzero revision is handled correctly.
4. **Durable replay protection.** Seen message and idempotency IDs are persisted and checked under a lock. Restart replay is rejected.
5. **Append-only audit receipts.** UPDATE and DELETE are aborted by triggers. Ledger rejections produce an UNCERTAIN receipt. No cross-database atomicity is claimed.
6. **Input validation.** Timezone-aware datetimes, positive sequences, expiry after timestamp, SHA format, and clock skew/age bounds are enforced.
7. **HALT integration.** Explicit halted flag rejects the envelope and records a rejection receipt. No catch-up after HALT.

execution_enabled remains False. External transport remains disabled.
