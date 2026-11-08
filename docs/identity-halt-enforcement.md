# SKIPPY AI Collaboration Bridge — Identity Verification HALT Enforcement

Phase 9 — Step 69E.3B.2F.3

Offline. Synthetic. Non-executing.

This document describes the coordinated HALT protection applied to
`ChiefIdentityVerifier` challenge issuance and signature verification.

## Identity guard binding

`ChiefIdentityVerifier.require_coordinated_guard(gate)` establishes an
explicit, controlled binding to a genuine `CoordinatedHaltGate`.

- Requires `type(gate) is CoordinatedHaltGate` (subclasses rejected).
- Rejects replaced `check` methods.
- Verifies that the gate’s `policy` and `protocol` are explicitly bound
  to the same gate instance (`coordinated_enforcement_required` and
  `coordinated_guard is gate`).
- Snapshots security-relevant configuration (paths, absent-legacy policy,
  omitted-source policy).
- Binding cannot be replaced through the supported method.
- Default construction remains ungated.
- The binding does not create a separate security authority; it consumes
  the existing coordinated gate.

Ungated (never-bound) identity verifiers cannot return successful
work-related verification.

## Challenge issuance protection

`issue_challenge()` evaluates coordinated protection before any database
write.

Rejection occurs for:

- Missing guard (never bound)
- Coordinated HALTED
- Coordinated UNKNOWN
- Missing or corrupted coordinated authority
- Failed migration
- Legacy policy or protocol HALT (observed as coordinated HALTED after
  migration)
- Guard identity mismatch
- Configuration drift
- Invalid or inconsistent gate results
- Replaced check method
- Invalid guard type

A rejected issuance creates no challenge record and no usable nonce.

## Verification protection

`verify()` requires a current successful coordinated evaluation before
consuming a challenge or returning a successful result.

On rejection:

- Returns `VerificationResult(verified=False, ...)`
- Does not consume the challenge
- Does not change authorization state
- Does not enable execution
- Does not expose internal exception details or credentials

Existing Ed25519 validation, timestamp handling, nonce uniqueness,
challenge scope checks, and replay prevention are unchanged.

## Key-registration policy

`register_public_key()` remains a synthetic, test-facing configuration
method. It does not require coordinated protection.

Synthetic fixture enrollment may occur before binding. This is isolated
because:

- No challenge can be issued and no successful verification can be
  returned until an explicit, valid coordinated binding is established
  and a current safety evaluation succeeds.
- The method does not accept private keys and does not enroll an AI
  identity as Chief.
- It is not exposed through any production collaboration interface.

No unrestricted bypass flag is introduced for production-facing identity
operations. No real Chief credential enrollment is performed.

## Protective revocation

`revoke()` remains available during coordinated HALT and during
coordinated authority failure.

Revocation is a protective operation. It does not require successful
work admission and does not clear HALT or restore keys.

## Lifecycle integration

`AuthorizationLifecycle.evaluate()` continues to enforce its own work
admission boundary.

When the lifecycle is constructed with a coordinated gate, the identity
verifier must be bound to the same gate. Challenge verification cannot
bypass either boundary. A rejected identity gate leaves the challenge
unconsumed and does not advance authorization state.

All successful synthetic results retain `execution_enabled=False`.

## Restart behavior

Coordinated guard binding is in-memory. After process restart a new
`ChiefIdentityVerifier` instance is unbound until
`require_coordinated_guard` is called again. Unbound instances reject
successful verification. Existing challenge records remain in the
database but cannot be successfully verified without rebinding.

## Known race conditions

Identity, authorization policy, and collaboration protocol use separate
SQLite databases. This implementation does not claim cross-database
atomicity. A race remains possible between the coordinated evaluation
and subsequent local writes. The design fails closed for execution.

## Remaining system-wide gaps

- Direct component entry points outside the coordinated gate remain
  ungated.
- No production startup integration.
- No automatic HALT clearing or autonomous recovery.
- Legacy HALT flags remain authoritative after migration.
- This is not a production cutover.

## Production-readiness limitations

- Offline and synthetic only.
- `execution_enabled=False` is mandatory on every result.
- No live trading, brokerage connectivity, financial transactions,
  Railway deployment, production database migration, or real credential
  enrollment.
- Binding must be re-established after every process start.
- Does not provide system-wide HALT enforcement.

All identity work-related operations require valid coordinated HALT
protection before proceeding.
