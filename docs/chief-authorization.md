# SKIPPY AI Collaboration Bridge — Chief Authorization (Step 69E.1)

Offline, non-executing authorization architecture.
Chief retains exclusive authority. AI components may propose and review.
They must not authorize or execute protected operations.

## Architecture

```
AuthorizationRequest (immutable, scoped)
        |
AuthorizationPolicy (deny by default)
        |
AuthorizationDecision (execution_enabled always False)
        |
authorization_events (local audit)
```

The policy lives in `skippy/collaboration/authorization.py` and does not
modify the existing collaboration protocol. Synthetic protocol approvals
remain synthetic and are not treated as authenticated Chief authorization.

## Roles

- Chief: sole authority for consequential actions.
- Skyppy, Jason, Hermes: propose, review, recommend. Cannot authorize.
- Bridge: routes messages. Cannot authorize.

## Protected operations

Read-only: repository reads, evidence reads, local analysis, local simulation.

Protected (execution disabled): GitHub writes, PR creation, branch changes,
merges, deployments, Railway, IFTTT, credentials, permissions, trading config,
order submission, financial transactions, autonomous execution.

Unknown actions are rejected.

## Approval lifecycle

PENDING → APPROVED → CONSUMED
PENDING → DENIED | EXPIRED | REVOKED

An approval is scoped to one action, one repository, one task, one digest,
and one expected SHA. A review approval never authorizes a merge. A
deployment approval never authorizes trading. An identity string is not
authentication.

## Identity verification

Not implemented in this milestone. Real Chief authentication is reserved
for Step 69E.2. The policy rejects AI impersonation of Chief and does not
accept sender names, chat text, repository comments, unsigned JSON, or
Boolean flags as proof.

## Replay and expiration

Consumption is one-time and conditional. A second consume fails. Expired
requests are marked EXPIRED. Revoked requests stay revoked.

## HALT

HALT persists in SQLite. It rejects decisions and consumption. It cannot
be overridden by an authorization record. There is no automatic resume.

## Audit

`authorization_events` records event id, timestamp, action, target,
previous and new state, and reason. No credentials are stored. The audit
is not claimed to be tamper-proof.

## Execution-disabled guarantee

Every decision returns `execution_enabled=False`, including a fully matching
APPROVED request. This milestone does not unlock protected operations.

## Remaining commissioning

Step 69E.2 must provide trusted Chief authentication and capture.
Until then, all records are synthetic test evidence.
