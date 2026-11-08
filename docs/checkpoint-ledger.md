# BRIDGE-01 Checkpoint Handoff Ledger

Local-only, fail-closed, append-only event ledger for offline checkpoint/state handoff.

## BRIDGE-01A corrections applied
- Stale implementation reports (even with new message IDs) cannot overwrite `CORRECTION_REQUESTED`.
- Explicit legal state transitions enforced; illegal sequences rejected.
- CI verification requires structured evidence matching the reviewed SHA.
- Authorization requires explicit separately-reviewed evidence; actor name alone is insufficient. PR and merge authorizations remain distinct.
- Ordinary work rejected after `HALTED` or `UNCERTAIN`. No automatic recovery.
- `expected_revision` required for state-changing operations.
- State projection covers assignment, source verification, CI pending/verified, PR opened, correction submitted, merge authorized, and merged.
- Identical redelivery is a no-op; conflicting reuse of the same identity is rejected.

## What it proves
- Local internal consistency of recorded events.
- Deterministic state projection from durable append-only history.
- Duplicate/stale report detection.
- Optimistic concurrency via required expected revision.
- Distinct recording of local test reports vs independently verified CI evidence.
- Distinct PR authorization vs merge authorization (neither inferred).

## What it does not prove
- External sender authenticity.
- Live provider delivery or receipt.
- Independent CI results until an explicit `CI_VERIFICATION_RECORDED` event with matching evidence is supplied.
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
