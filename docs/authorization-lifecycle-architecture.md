# SKIPPY AI Collaboration Bridge — Authorization Lifecycle (Step 69E.3A)

Offline, synthetic, non-executing integration boundary.
`execution_enabled` remains `False` on every result.

## Component boundaries

```
Signed challenge
        |
ChiefIdentityVerifier   (synthetic key possession)
        |
AuthorizationPolicy     (lifecycle rules, HALT, terminal states)
        |
CollaborationProtocol   (independent HALT)
        |
LifecycleResult         (execution_enabled=False)
```

The bridge lives in `skippy/collaboration/authorization_lifecycle.py`.
It does not modify the protected execution architecture.

## Integration sequence

1. Check CollaborationProtocol HALT. Fail closed if set.
2. Check AuthorizationPolicy HALT. Fail closed if set.
3. Verify the signed challenge against the trusted challenge registry.
4. Evaluate the authorization policy for the same scope via the non-executing `evaluate_evidence` path (PENDING records only; no state transition).
5. Return an immutable result. Do not transition the authorization.
6. Do not enable execution.

## Evidence-to-authorization mapping

A valid signature establishes that the synthetic signing key signed the
trusted challenge. It does not establish real Chief enrollment or informed
human consent. An APPROVED policy state is not authenticated Chief
authorization. Sender fields, identity strings, and unsigned messages are
not evidence.

## Lifecycle states

The bridge rejects terminal states: CONSUMED, REVOKED, EXPIRED, DENIED.
It does not overwrite them. A matching PENDING request with valid synthetic
evidence returns `accepted=True` with `execution_enabled=False`. It does
not transition the request to APPROVED.

## Replay prevention

The verifier consumes a challenge on successful verification. A second
evaluation of the same signature is rejected. A consumed authorization is
rejected by the policy. These checks are separate.

## Database consistency

The verifier, the policy, and the protocol use separate SQLite databases.
This bridge does not claim atomicity across them. If verification succeeds
and the subsequent policy evaluation fails, the challenge may already be
consumed. Execution remains disabled. That is fail-closed, not atomic.

## Failure handling

Invalid signatures, scope mismatches, unknown authorizations, terminal
states, expired challenges, and revoked keys fail closed. There are no
automatic retries and no automatic recovery.

## HALT coordination

The bridge fails closed if either component reports HALT. There is no
automatic reset. A race remains possible between the two checks because
they are separate databases. Coordinated HALT is a commissioning blocker.

## Remaining commissioning blockers

- Real Chief enrollment and approval capture are not implemented.
- Execution remains disabled.
- Cross-component HALT coordination is unresolved.
- Cross-database atomicity is not guaranteed.
- Hermes is not integrated and has no execution privilege.
- Synthetic keys must not authorize real operations.

This checkpoint introduces no credentials, no network calls, and no
execution paths.
