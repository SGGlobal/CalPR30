# SKIPPY AI Collaboration Bridge — Work-Admission Binding (Step 69E.3B.2F.2)

**Status:** Offline, synthetic, non-executing. Not system-wide HALT enforcement. Not a production cutover. Not production-ready.

## Admission requirements

`CollaborationOrchestrator.run()` and `AuthorizationLifecycle.evaluate()` now refuse synthetic work unless coordinated protection is established and currently valid.

A non-null guard object, a constructor argument, a previous successful check, or a caller-supplied result is not sufficient. Admission re-evaluates the bound guard.

## Orchestrator

`CollaborationOrchestrator.run()` calls `CollaborationProtocol.admit_work()` before creating assignments, running handlers, dispatching, or advancing task state.

`admit_work()` requires:

- coordinated enforcement is still required
- the original guard instance is still bound
- policy, protocol, paths, and migration settings match the snapshot
- the current gate evaluation succeeds
- coordinated status is `NOT_HALTED`
- migration completed
- neither legacy component is HALTED
- `execution_enabled` is false

Failure raises `ProtocolError("Work admission rejected")`. An orchestrator constructed before binding cannot bypass this: `run()` checks at call time.

`recover()` and `report()` remain read-only and are not work admission.

## Lifecycle

`AuthorizationLifecycle.evaluate()` requires the originally injected gate. Construction without a gate rejects evaluation. Replacing the gate reference after construction rejects evaluation. Alignment, result consistency, and successful evaluation are required before challenge consumption or policy evaluation.

Rejection does not consume the identity challenge and does not advance authorization state. A successful result remains synthetic. `execution_enabled` stays false.

## Restart

Guard binding is in-memory. Reopening a protocol or constructing a lifecycle without rebinding rejects work admission. This is intentional.

## Remaining gaps

This is not system-wide enforcement. Direct protocol `accept()`, ungated policy methods, identity issuance and verification, router dispatch, TradingView ingestion, and tool execution remain outside this admission boundary unless they separately opt in.

A successful admission check is still point-in-time. A HALT after the check and before the local transaction remains a cross-database race. No cross-database atomicity is claimed.

Protective `halt()`, `revoke()`, task-level `MessageType.HALT`, uncertainty marking, and read-only recovery remain available.

No production startup wiring was added.
