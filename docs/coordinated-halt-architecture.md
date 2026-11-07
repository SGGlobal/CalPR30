# SKIPPY AI Collaboration Bridge — Coordinated HALT Architecture (Step 69E.3B.1)

**Status:** Architecture-only. No production HALT changes in this checkpoint.  
**execution_enabled remains False.**  
**No real Chief enrollment, no GitHub writes, no trading, no protected execution.**

## 1. Current independent HALT interfaces

### AuthorizationPolicy (`skippy/collaboration/authorization.py`)
- Persistent flag in its own SQLite database: table `authorization_control`, key `'halted'`, value `'1'`.
- Methods:
  - `halt()` — writes the flag inside a transaction (`BEGIN IMMEDIATE`) and records an audit event.
  - `is_halted()` — reads the flag.
- Checked inside `decide()` and `evaluate_evidence()` (both open their own `BEGIN IMMEDIATE` transaction, read the flag first, fail closed with `AuthorizationDecision(allowed=False, state=DENIED, reason="System is halted")`, `execution_enabled=False`).
- Does **not** observe CollaborationProtocol HALT.
- No automatic resume.

### CollaborationProtocol (`skippy/collaboration/protocol.py`)
- Persistent flag in its own SQLite database: table `control`, key `'halted'`, value `'1'`.
- Methods:
  - `halt()` — writes the flag inside `BEGIN IMMEDIATE`.
  - `is_halted` (property) — refreshes from SQLite on every read (`_refresh_halt`).
- Checked inside `accept()` and outbound request paths (read inside the acceptance transaction so a second process cannot accept new work after another process has halted).
- Raises `ProtocolError` when halted (except for an explicit HALT message).
- Does **not** observe AuthorizationPolicy HALT.
- No automatic resume.

### ChiefIdentityVerifier (`skippy/collaboration/chief_identity.py`)
- No HALT flag or control table.
- Challenge registry and trusted-key tables only.
- Verification is independent of any HALT state.

### AuthorizationLifecycle (`skippy/collaboration/authorization_lifecycle.py`)
- Bridge only. Checks **both** independent flags sequentially:
  1. `protocol.is_halted`
  2. `policy.is_halted()`
- Fails closed (returns `LifecycleResult(accepted=False, execution_enabled=False)`) if either is set.
- Records a non-mutating lifecycle audit.
- **Race window:** the two checks are separate database connections. A HALT written to one component between the two reads is not atomic across components.
- Does not write either HALT flag.

## 2. SQLite persistence and transaction boundaries

| Component              | Database          | Control table              | Transaction style                  | Atomic with other components? |
|------------------------|-------------------|----------------------------|------------------------------------|-------------------------------|
| AuthorizationPolicy    | policy.db         | authorization_control      | BEGIN IMMEDIATE on decide/evaluate | No                            |
| CollaborationProtocol  | protocol.db       | control                    | BEGIN IMMEDIATE on accept/halt     | No                            |
| ChiefIdentityVerifier  | identity.db       | (none)                     | BEGIN IMMEDIATE on verify/issue    | No                            |

All three databases are separate files. There is no shared transaction, no two-phase commit, and no cross-database trigger.

## 3. Failure behavior today

- Each component fails closed on **its own** HALT only.
- Lifecycle fails closed if **either** reports HALT, but the checks are not atomic.
- A process that only talks to AuthorizationPolicy (or only to CollaborationProtocol) can still proceed if the other component is halted.
- Restarting a process reloads the flag from SQLite (persistent), but there is no coordinated view.
- No automatic recovery or resume path exists in any component.

## 4. Design goal for coordinated HALT

A HALT must be impossible to bypass by independently operating collaboration, identity, or authorization components.

Required properties (to be implemented in a later, separately reviewed milestone):

1. **Single source of truth** for the emergency HALT flag, persisted and readable by every component that can accept work or evaluate evidence.
2. **Fail-closed on any HALT** — if the coordinated flag is set, AuthorizationPolicy.decide/evaluate_evidence, CollaborationProtocol.accept/outbound, and AuthorizationLifecycle.evaluate must all reject new work.
3. **No bypass path** — a component must not be able to accept or evaluate work solely because its local flag is clear.
4. **Persistence across restarts** and across process boundaries.
5. **Audit** — every HALT set, clear (when eventually allowed), and rejection caused by HALT is recorded. Audit must not store secrets.
6. **execution_enabled remains False** in all results. HALT never enables execution.
7. **No automatic resume.** Clearing HALT is an explicit, separately authorized Chief action (future milestone).
8. **Commissioning blocker remains** until the coordinated path is implemented, tested, and reviewed.

## 5. Proposed architecture (not implemented)

```
                    +---------------------------+
                    |  CoordinatedHaltStore     |
                    |  (single SQLite file or   |
                    |   shared control table)   |
                    |  key='halted' value='1'   |
                    +-------------+-------------+
                                  |
          +-----------------------+-----------------------+
          |                       |                       |
   AuthorizationPolicy    CollaborationProtocol   AuthorizationLifecycle
   (reads coordinated)    (reads coordinated)     (reads coordinated)
          |                       |                       |
   local audit only       local audit only        non-mutating audit
```

Options (to be decided in a later implementation checkpoint):

- **A. Shared control database** — one small SQLite file that all three components open for the HALT flag only. Existing per-component databases remain for their primary data.
- **B. HALT service / facade** — a thin CoordinatedHalt object injected into Policy, Protocol, and Lifecycle. It owns the single database connection for the flag.
- **C. Dual-write with verification** — each component writes its own flag **and** a shared flag; readers require the shared flag to be clear. Higher complexity; only if shared file is unacceptable.

Preferred direction for the next milestone: **B (injected facade over a single control database)** so that existing component constructors change minimally and the HALT source of truth is explicit.

## 6. Methods that currently read or modify HALT independently

| Method                          | Component             | Reads own flag | Writes own flag | Observes other flag |
|---------------------------------|-----------------------|----------------|-----------------|---------------------|
| AuthorizationPolicy.halt        | Policy                | —              | Yes             | No                  |
| AuthorizationPolicy.is_halted   | Policy                | Yes            | —               | No                  |
| AuthorizationPolicy.decide      | Policy                | Yes            | —               | No                  |
| AuthorizationPolicy.evaluate_evidence | Policy          | Yes            | —               | No                  |
| CollaborationProtocol.halt      | Protocol              | —              | Yes             | No                  |
| CollaborationProtocol.is_halted | Protocol              | Yes            | —               | No                  |
| CollaborationProtocol.accept    | Protocol              | Yes            | —               | No                  |
| AuthorizationLifecycle.evaluate | Lifecycle             | Both (seq.)    | —               | Both (race window)  |
| ChiefIdentityVerifier.*         | Identity              | No             | No              | No                  |

## 7. Constraints preserved

- This checkpoint contains **no production code changes**.
- `execution_enabled=False` everywhere.
- Separate databases for primary data remain.
- Terminal states, challenge single-use, and existing fail-closed paths are unchanged.
- Coordinated HALT is still a commissioning blocker until implemented.

## 8. Next milestone (out of scope for 69E.3B.1)

Implement the chosen coordinated HALT store, inject it, update the three components to consult it, add regression tests that prove a HALT set through any entry point is observed by all others, and re-confirm the 470-test baseline.
