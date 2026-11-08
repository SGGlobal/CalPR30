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
