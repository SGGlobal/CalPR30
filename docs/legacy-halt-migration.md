# SKIPPY AI Collaboration Bridge — Legacy HALT Migration Safeguards (Step 69E.3B.2B)

**Status:** Isolated offline helper. Not integrated into live startup. Not a production cutover.

## Purpose

Preserve existing HALT states when transitioning from the independent
`AuthorizationPolicy` and `CollaborationProtocol` HALT flags to the
`CoordinatedHalt` authority.

## Behavior

- Reads legacy HALT records with **read-only** SQLite connections (`mode=ro`).
- If either legacy component is HALTED, calls `CoordinatedHalt.activate_halt("migration")`.
- Uses the coordinated authority's existing controlled reason-code allowlist (`migration`).
- Never clears or resets a legacy HALT record.
- Idempotent across repeated attempts and process restarts.
- A missing or unreadable coordinated authority is **never** recreated as `NOT_HALTED`.
- Absent legacy databases are unsafe by default. Callers must explicitly select
  `AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE` to treat a missing file as not halted.
- Corrupted databases, incompatible schemas, unreadable records, lock failures,
  and uncertain HALT values fail closed.
- Partial failure (one source unreadable) does not activate the coordinated authority.

## Security boundaries

- `execution_enabled=False`. No method grants protected execution.
- No live trading, financial transactions, Railway deployment, or production migration.
- No automatic HALT reset. No autonomous recovery.
- Audit evidence is the coordinated authority's existing best-effort secondary audit.
  Free-form text and credentials are not persisted.
- This helper is isolated. It is not wired into component construction or startup.

## Remaining limitations

- Not a cross-database atomic transaction. Legacy reads and coordinated activation
  are separate. A crash between a successful legacy read and coordinated commit
  is safe to retry because activation is idempotent and legacy records are never cleared.
- Secondary audit writes remain best-effort and non-tamper-proof.
- Absence-is-safe is an explicit caller policy, not a discovered fact.
- Does not perform schema migration of authorization requests, messages, or other
  component data. HALT flag preservation only.
