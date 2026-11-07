# SKIPPY AI Collaboration Bridge — Coordinated HALT Integration Safety Gate (Step 69E.3B.2C.1)

**Status:** Isolated, opt-in integration checkpoint. Not a production cutover. Not production-ready.

## Integration architecture

```
AuthorizationLifecycle.evaluate()          [default path unchanged]
        |
        +-- optional CoordinatedHaltGate   [opt-in only]
                |
                +-- LegacyHaltMigration.migrate()   [performed by the gate]
                +-- CoordinatedHalt.read_status()   [must be NOT_HALTED]
                +-- AuthorizationPolicy.is_halted() [legacy remains authoritative]
                +-- CollaborationProtocol.is_halted [legacy remains authoritative]
        |
        +-- existing legacy HALT checks
        +-- ChiefIdentityVerifier.verify()          [challenge consumption]
        +-- AuthorizationPolicy.evaluate_evidence() [synthetic only]
```

The gate is supplied explicitly to `AuthorizationLifecycle`. Construction without a gate preserves existing behavior.

The gate performs migration itself. A caller-supplied `MigrationResult` is not an input and is never treated as authorization. Status is re-read after migration.

## Fail-closed behavior

Evaluation is rejected when any of the following is true:

- Legacy migration fails or is incomplete.
- Coordinated authority is missing, corrupted, unreadable, or reports `HALTED` or `UNKNOWN`.
- Invalid coordinated schema or database/lock errors.
- Either legacy component is HALTED.
- Legacy HALT state cannot be read.

A failed migration is never converted into permission to proceed. A coordinated `NOT_HALTED` reading never bypasses a legacy HALT. `execution_enabled` remains `False` on every outcome.

The gate runs before identity challenge consumption and policy evaluation. A failed gate does not consume the challenge.

## Current limitations

- This is not system-wide HALT enforcement.
- Not integrated into live component startup.
- No production migration. No automatic HALT reset. No autonomous recovery.
- No protected execution. No real approval. No credential enrollment.
- Secondary audit remains best-effort and non-tamper-proof.
- Not a cross-database atomic transaction.

## Remaining direct component entry points

These paths do not consult the gate in this checkpoint:

- `AuthorizationPolicy.decide()`, `consume()`, `submit()`, and `halt()`
- `CollaborationProtocol.accept()`, outbound tracking, and `halt()`
- `ChiefIdentityVerifier.verify()` called directly
- `AuthorizationLifecycle.evaluate()` without an injected gate
- `CoordinatedHalt` used alone

Do not describe those paths as gated.

## Mixed-version process risks

- A process using the gate and a process not using the gate can disagree.
- One process can observe legacy HALT while another observes only coordinated `NOT_HALTED`.
- Migration in one process does not fence direct entry points in another process.
- Restart is safe for the gate because checks are repeated, but mixed-version processes remain a commissioning blocker.

## Requirements for a future coordinated-only cutover

Not authorized by this checkpoint. A future cutover would require, at minimum:

- Every direct entry point above consults the coordinated authority or the gate.
- Legacy flags remain authoritative until the cutover is explicitly proven.
- No coordinated-only operation while any ungated entry point remains.
- Separate production migration authorization.
- No automatic HALT clearing.
- Explicit handling of mixed-version processes.
- `execution_enabled=False` until a later, separately authorized execution milestone.

## Known cross-database race conditions

- Legacy reads and coordinated activation are separate transactions.
- The gate re-checks legacy flags after migration, but a HALT written between those checks and later policy evaluation is a race.
- `AuthorizationPolicy`, `CollaborationProtocol`, `ChiefIdentityVerifier`, and `CoordinatedHalt` use separate databases. There is no cross-database atomicity.
- A crash between migration and a later evaluation is safe to retry. It is not a single atomic decision.
