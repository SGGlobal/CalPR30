# SKIPPY AI Collaboration Bridge — Legacy HALT Migration Safeguards (Step 69E.3B.2B.1)

**Status:** Isolated offline helper. Not integrated into live startup. Not a production cutover.

## Purpose

Preserve existing HALT states when transitioning from the independent
`AuthorizationPolicy` and `CollaborationProtocol` HALT flags to the
`CoordinatedHalt` authority.

## Behavior

- Reads legacy HALT records with **read-only** SQLite connections (`mode=ro`).
- If either examined legacy component is HALTED, calls `CoordinatedHalt.activate_halt("migration")`.
- Uses the coordinated authority's existing controlled reason-code allowlist (`migration`).
- Never clears or resets a legacy HALT record.
- Idempotent across repeated attempts and process restarts.
- A missing or unreadable coordinated authority is **never** recreated as `NOT_HALTED`.
- **Omitted sources are unsafe by default.** Passing `None` for a legacy path fails closed unless the caller explicitly selects `OmittedSourcePolicy.OMITTED_SOURCE_IS_AUTHORIZED`. A migration does not report success when a required source was never examined.
- Absent legacy databases are unsafe by default. Callers must explicitly select `AbsentLegacyPolicy.ABSENT_LEGACY_IS_SAFE` to treat a **genuinely missing** path as not halted.
- Absence-is-safe applies only when the path does not exist. Directories, dangling symlinks, sockets, and other non-file objects are invalid and fail closed even under the absence-is-safe policy.
- Corrupted databases, incompatible schemas, unreadable records, lock failures, and uncertain HALT values fail closed.
- Partial failure (one source unreadable) does not activate the coordinated authority. `CoordinatedHalt.activate_halt()` semantics are unchanged.

## Partial migration failure semantics

Scenario: `AuthorizationPolicy` is HALTED, `CollaborationProtocol` is unreadable, and `CoordinatedHalt` currently reports `NOT_HALTED`.

Current isolated-helper behavior:

- Migration raises `MigrationError`.
- The coordinated authority is **not** activated and remains `NOT_HALTED`.
- The legacy AuthorizationPolicy HALT record is preserved (read-only).
- Success is never reported on incomplete evidence.

This is fail-closed **for the isolated helper**: the caller cannot treat the attempt as a successful migration. It is adequate only while the helper is not the sole HALT authority.

Safest behavior for future integration (not implemented here):

- Do **not** treat a failed migration as `NOT_HALTED`.
- Until a complete successful migration result is obtained, continue to consult the legacy flags, or treat `MigrationError` as deny-work.
- Do not switch live components to coordinated-only checks after a partial failure.
- Activating the coordinated authority on partial evidence was considered and rejected for this checkpoint: it would change activation timing, and a later successful read of the second source could not undo a HALT. The current exception-only approach preserves the option to repair the unreadable source and retry without having activated on incomplete evidence. Coordinated activation semantics remain unchanged.

## Security boundaries

- `execution_enabled=False`. No method grants protected execution.
- No live trading, financial transactions, Railway deployment, or production migration.
- No automatic HALT reset. No autonomous recovery.
- Audit evidence is the coordinated authority's existing best-effort secondary audit. Free-form text and credentials are not persisted.
- This helper is isolated. It is not wired into component construction or startup.

## Remaining limitations

- Not a cross-database atomic transaction. Legacy reads and coordinated activation are separate. A crash between a successful legacy read and coordinated commit is safe to retry because activation is idempotent and legacy records are never cleared.
- Secondary audit writes remain best-effort and non-tamper-proof.
- Absence-is-safe and omitted-source authorization are explicit caller policies, not discovered facts.
- A partial failure leaves the coordinated authority `NOT_HALTED` even if one legacy source is HALTED. That is safe only while legacy flags remain authoritative for any component that has not completed migration.
- Does not perform schema migration of authorization requests, messages, or other component data. HALT flag preservation only.
