# SKIPPY AI Collaboration Bridge — Direct Authorization Entry-Point HALT Guard (Step 69E.3B.2D.1)

**Status:** Opt-in, offline, synthetic, non-executing. Not a production cutover. Not production-ready.

## Direct entry-point inventory

| Method | Effect | Guarded when opted in? | Rationale |
| --- | --- | --- | --- |
| `submit()` | Creates an authorization record | Yes | Work acceptance |
| `decide()` | Evaluates and may advance state | Yes | Work evaluation |
| `evaluate_evidence()` | Evaluates PENDING evidence | Yes | Work evaluation |
| `consume()` | Consumes authorization state | Yes | Work acceptance |
| `halt()` | Administrative HALT write | No | Protective operation; remains available |
| `revoke()` | Terminal protective revocation | No | Protective operation; remains available |
| `record_lifecycle_audit()` | Non-mutating audit | No | Needed to record rejections |
| `is_halted()`, `stored_state()`, `events_for()`, `classify()` | Reads / classification | No | Not work acceptance |

Ungated instances are not coordinated-protected.

## Opt-in mechanism

`AuthorizationPolicy.require_coordinated_guard(gate)` binds a `CoordinatedHaltGate` that must reference the same policy instance. Binding records that coordinated enforcement is required, retains the original guard instance identity, and snapshots the policy, protocol, database locations, coordinated authority path, and migration policy settings. A replacement with identical configuration is rejected. Once required, a missing, replaced, or inconsistent guard fails closed. Supported methods cannot disable the guard. Default construction remains ungated. This does not claim protection against arbitrary modification of the entire Python process.

The guard is invoked before any local policy write lock. It performs migration itself, requires coordinated `NOT_HALTED`, completed migration, and no legacy HALT. Failed, malformed, contradictory, or exceptional results reject. Exception details are not exposed. A successful result does not grant protected execution.

## Fail-closed direct operations

- `submit()` raises `AuthorizationError` before creating a record.
- `decide()`, `evaluate_evidence()`, and `consume()` return a denied `AuthorizationDecision` before advancing or consuming state.
- Local HALT checks inside the existing transactions remain in place.
- Rejection is audited best-effort without changing authorization state.

## Transaction and race limitations

The gate reads and may write other databases. It is called before `BEGIN IMMEDIATE` on the policy database to avoid holding a policy write lock across migration. There is no cross-database atomicity.

A HALT written after a successful guard check and before the subsequent local authorization transaction is a race. This checkpoint documents that limitation rather than claiming an atomic fence. Stronger atomicity would require an architectural change and is not implemented here.

## Remaining unprotected entry points

- `CollaborationProtocol.accept()` and outbound tracking
- Direct `ChiefIdentityVerifier.verify()`
- `AuthorizationLifecycle.evaluate()` without an injected gate
- `AuthorizationPolicy` instances that have not opted in
- Administrative `halt()` and `revoke()` by design

## Future system-wide enforcement

Not authorized here. Would require every work-acceptance entry point to consult the coordinated authority, mixed-version processes to be fenced, legacy flags to remain authoritative until proven, separate production migration authorization, and no automatic HALT clearing.

`execution_enabled=False` remains mandatory.
