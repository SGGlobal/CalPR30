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

## BRIDGE-01B integrity hardening
- Complete message-envelope identity compared for duplicates (project, task, checkpoint, actor). Conflicting reuse across identities rejected. Exact duplicate remains no-op.
- Source reviews must reference the current implementation SHA. CI verification requires a prior matching source review and is bound to that SHA. A new SHA invalidates prior CI evidence for the new revision.
- Authorization requires structured, separately reviewed evidence (`authorization_id` + `reviewed`). Actor name or truthy values alone are insufficient. PR and merge authorizations remain independent.
- Inconsistent checkpoint identifiers within the same active work item are rejected. Legal progression through review → CI → PR authorized → PR opened → merge authorized → merged is reachable. Rejected transitions leave the ledger unchanged.

## Offline authenticity limitation
The ledger records supplied evidence. It cannot independently establish external sender authenticity, live provider delivery, or that a submitted CI run actually occurred on GitHub. Those require a future separately reviewed adapter.

## BRIDGE-01C targeted integrity
- When both message ID and idempotency key are supplied, both identities are verified and must resolve to the same event before an identical-duplicate no-op is returned. Mixed identifiers are rejected.
- CI verification requires an explicitly source-verified review of the current implementation SHA. Both the reviewed SHA and the CI SHA must match the current implementation. A review without source verification cannot establish CI-verified status. After a new SHA, prior CI evidence is rejected.
- CI_PENDING remains in the status enum as a reserved state but is not automatically entered; it may be reached only by a future explicit controlled transition. Supplied authorization and CI evidence are records, not independently authenticated external facts.

## Offline authenticity limitation (reaffirmed)
The ledger records supplied evidence. It cannot independently establish external sender authenticity or that a submitted CI run actually occurred.

## Final assignment-state and mixed-identifier corrections
- ASSIGNMENT_CREATED is represented in both state-projection methods. A newly recorded assignment does not appear as UNCERTAIN.
- Multiple conflicting assignments or checkpoint identifiers under the same active project/task are rejected.
- Mixed message ID / idempotency key pairs (first event's message ID + second event's key) produce a pair-mismatch rejection even when envelope content is otherwise identical. Exact duplicate redelivery remains a no-op.
