# BRIDGE-01 Checkpoint Handoff Ledger

Local-only, fail-closed, append-only event ledger for offline checkpoint/state handoff.

## What it proves
- Local internal consistency of recorded events.
- Deterministic state projection from durable append-only history.
- Duplicate/stale report detection via message_id and idempotency_key.
- Optimistic concurrency via expected revision.
- Distinct recording of local test reports vs independent CI verification.
- Distinct PR authorization vs merge authorization (neither inferred).

## What it does not prove
- External sender authenticity.
- Live provider delivery or receipt.
- Independent CI results until an explicit CI_VERIFICATION_RECORDED event is supplied.
- Chief authorization (events are evidence only; Chief separately approves protected actions).
- Cross-database atomicity.
- Permission to open PRs, merge, or execute.

## Constraints
- Offline and synthetic only.
- execution_enabled=False.
- No external AI APIs, network listeners, Railway, live trading, brokerage, production migration, or protected execution.
- No automatic HALT clearing or state advancement on UNKNOWN.
- Independent of the pending identity verification halt enforcement branch.

See `skippy/collaboration/checkpoint_ledger.py` and `tests/test_checkpoint_ledger.py`.
