# NIGHTSHIFT-01 Bounded Offline Runner

Thin, offline, non-executing orchestration layer for one simulated Chief assignment, one simulated Jason result, one simulated SKIPPY review, and at most one correction.

## What it does
- Reuses `CollaborationOrchestrator`, `CollaborationProtocol`, `CheckpointLedger`, and the existing coordinated HALT gate.
- Validates prerequisites (genuine guard, not HALTED/UNKNOWN) before creating work.
- Records revision-aware ledger events.
- Emits a durable, restart-safe, clearly synthetic morning report.
- Fail-closed on missing guard, HALT, duplicate runner, and ledger/protocol errors.
- Never claims independent CI, PR permission, or merge permission.

## What it does not do
- Does not call external AI APIs, network, shell, git, or GitHub.
- Does not enable execution or clear HALT.
- Does not claim cross-database atomicity.
- Does not self-heal, retry, or run unattended.
- Does not replace existing authorities.

## Example reports

### Successful synthetic handoff
```json
{
  "assignment_id": "ex-success",
  "state": "SOURCE_VERIFIED",
  "outcome": "synthetic-complete",
  "ci_verified": false,
  "synthetic": true,
  "execution_enabled": false,
  "pending_chief_decisions": ["PR authorization", "merge authorization"]
}
```

### Fail-closed (missing guard)
```json
{
  "assignment_id": "ex-fail",
  "state": "REFUSED",
  "outcome": "fail-closed",
  "refusal_reason": "Missing coordinated guard",
  "execution_enabled": false,
  "synthetic": true
}
```

See `skippy/collaboration/nightshift_runner.py` and `tests/test_nightshift_runner.py`.

## NIGHTSHIFT-01 corrections applied
- Correction cycles record correction submission and final review. Successful completion is never reported while the ledger remains in CORRECTION_REQUESTED.
- Morning reports are persisted (assignment ID, outcome, correction count, refusal reason, final state). Restart reconstructs the same factual report without re-running work. Fail-closed outcomes are recorded durably when storage is available.
- Before recording an assignment, the runner verifies that the supplied coordinated guard is the existing guard bound to the protocol. Missing, mismatched, replaced, HALTED, or UNKNOWN guards prevent work from being dispatched.
- Step budget and monotonic timeout are enforced during processing. Deterministic clock and step-limit test doubles are supported.

## Example reports (post-correction)

### Successful synthetic handoff
```json
{
  "assignment_id": "ex-success",
  "state": "SOURCE_VERIFIED",
  "outcome": "synthetic-complete",
  "correction_count": 0,
  "ci_verified": false,
  "synthetic": true,
  "execution_enabled": false
}
```

### Fail-closed (missing guard)
```json
{
  "assignment_id": "ex-fail",
  "state": "REFUSED",
  "outcome": "fail-closed",
  "refusal_reason": "Missing coordinated guard",
  "execution_enabled": false,
  "synthetic": true
}
```

## Final integrity corrections
- Historical reports are never overwritten. Unique assignment identity is enforced. An identical duplicate request does not erase or change the original outcome. Conflicting replays are rejected.
- The runner inspects the actual result from CollaborationOrchestrator.run() before recording a successful final review. Expected task identity and terminal COMPLETE status are required. Incomplete results are reported as UNCERTAIN. SOURCE_VERIFIED is not manufactured from hardcoded fixtures.
- Step and timeout limits are validated against fixed safe maximums at construction. Elapsed time is measured from the injected clock. Synchronous orchestrator and ledger operations cannot be interrupted mid-call; checks occur before each major step.

## Evidence that duplicates cannot corrupt completed reports
A successful report is persisted once. A subsequent submission with the same assignment ID either is a no-op (identical outcome) or raises Conflicting replay. After restart, report() returns the original outcome, state, and correction count unchanged.

## NIGHTSHIFT-01B targeted corrections
- Assignment identity is checked before dispatch. Identical redelivery (same project/task/checkpoint) returns the existing report without new work. Conflicting reuse of an assignment ID fails closed. The completed report is preserved. Verified with a newly constructed runner after restart.
- Timeout uses an injected monotonic clock. The deadline is validated before final completion. Synchronous operations cannot be interrupted mid-call (documented).
- Orchestrator completion requires matching task ID, COMPLETE status, and absence of HALT. Incomplete or partial results are reported as UNCERTAIN. Source verification is recorded only when the expected synthetic review evidence is present.

## Synchronous operation limitation
Orchestrator and ledger calls are synchronous. A timeout check occurs before each major step and before final completion, but an in-flight call cannot be interrupted.

## Final requirement corrections
- Default elapsed-time source is `time.monotonic()`. Injectable monotonic clock remains available for deterministic tests. Wall-clock changes cannot bypass the timeout.
- Assignment identity includes objective and correction settings. Identical redelivery returns the existing report without dispatch. Conflicting reuse of an assignment ID fails closed and preserves the original report.
- Completion requires matching task ID, COMPLETE status, absence of HALT, and expected synthetic review evidence before recording source verification. Partial persistence is reported as UNCERTAIN, distinct from a clean pre-admission refusal. No automatic continuation.

## Remaining requirement completions
- Before recording SOURCE_VERIFIED, the runner requires matching task ID, COMPLETE status, approved review outcome, empty unacknowledged collection, no HALT, and correction-count consistency. Incomplete or contradictory results are marked UNCERTAIN.
- Partial persistence (ledger or protocol operation has started) is reported as UNCERTAIN, distinct from a clean pre-admission refusal. Original evidence is preserved. No automatic continuation.
- Refused reports persist the original objective and correction settings. Identical redelivery after restart returns the original report. Conflicting redelivery is rejected.

## Completion-validation correction
- Before recording SOURCE_VERIFIED, the runner requires `orch_result["outcome"] == "approved"`. Missing or unexpected review outcomes are rejected as UNCERTAIN.
- The orchestrator's correction count must match the recorded NIGHTSHIFT correction count.
- `unacknowledged` must be empty and `halted` must be false, using the returned task report.
- Contradictory completion evidence is classified as UNCERTAIN.
