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
