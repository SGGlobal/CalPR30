# SKIPPY AI Collaboration Bridge — Coordinated HALT Architecture (Step 69E.3B.1A)

**Status:** Architecture-only. Documentation checkpoint.  
**No production code changes in this commit.**  
**execution_enabled remains False on every path.**  
**No automatic HALT reset. No real Chief enrollment. No protected execution.**

Verified integration baseline: 470 tests passed.  
Preferred direction (accepted by Skyppy): injected `CoordinatedHalt` facade over a single SQLite control database.

Existing verified behavior is clearly separated from proposed behavior below.

---

## 1. Threat model

### Assets to protect
- Consequential actions (GitHub writes, deployments, trading, credential changes, autonomous execution).
- Authorization state transitions that could be interpreted as approval.
- Challenge consumption that could be used to claim evidence.
- Audit integrity of HALT events.

### Adversaries / failure modes considered
- Compromised or buggy component that continues after HALT is requested.
- Multiple processes or restarts that observe a stale local flag.
- Mixed-version processes during migration.
- Database unavailability, corruption, lock timeout, or crash.
- Audit-write failure that must not erase a committed HALT.
- Operator error or partial migration that leaves legacy and coordinated stores inconsistent.

### Security goals
- A HALT, once durably activated, cannot be bypassed by independently operating collaboration, identity, or authorization components.
- UNKNOWN or unreadable HALT state fails closed.
- No method may grant execution permission.
- No automatic recovery or resume.

### Non-goals (this architecture)
- Real Chief enrollment or biometric/hardware confirmation.
- Enabling any protected execution path.
- Tamper-proof or append-only external audit log (best-effort secondary audit only).
- Cross-database atomicity between primary component databases.
- Automatic cancellation of already-started external side effects (future execution fencing required before any external side effects are commissioned).

---

## 2. Recommended architecture

**Injected CoordinatedHalt facade over a single authoritative SQLite control database.**

```
                    +-----------------------------------+
                    |  CoordinatedHalt                  |
                    |  (owns single control.db)         |
                    |  schema_version, halt_state,      |
                    |  monotonic sequence, timestamps   |
                    +----------------+------------------+
                                     |
          +--------------------------+--------------------------+
          |                          |                          |
   AuthorizationPolicy       CollaborationProtocol      AuthorizationLifecycle
   (injected reader)         (injected reader)          (injected reader)
          |                          |                          |
   existing local DB         existing local DB          non-mutating audit
   (auth requests)           (tasks, messages)          (policy events)
```

- Primary data stays in the existing per-component SQLite files.
- The HALT source of truth is exclusively the shared control database.
- Components receive a `CoordinatedHalt` instance at construction (or a narrow reader interface).
- Legacy `AuthorizationPolicy.halt()` and `CollaborationProtocol.halt()` become thin adapters that call `activate_halt()` on the coordinated authority and then update their local audit only.
- ChiefIdentityVerifier continues to have no HALT flag of its own; any future verification path that could accept work must consult the coordinated authority (via the lifecycle bridge or an explicit check).

Existing verified behavior (unchanged in this checkpoint):
- Each component currently maintains an independent persistent flag in its own database.
- Lifecycle checks both flags sequentially (race window).
- `execution_enabled=False` everywhere.
- No automatic resume.

---

## 3. Persistent HALT store (Task 1)

### Authoritative database
Single file: `coordinated_halt.db` (path supplied at construction; directory must exist).

### Schema (proposed)

```sql
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS halt_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state     TEXT NOT NULL CHECK (state IN ('NOT_HALTED', 'HALTED')),
    sequence  INTEGER NOT NULL CHECK (sequence >= 0),
    updated_at TEXT NOT NULL,
    reason    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS halt_events (
    event_id   TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sequence   INTEGER,
    detail     TEXT NOT NULL
);
```

### Explicit initialized states and safe initialization
- **Ordinary startup must never recreate a missing HALT database as NOT_HALTED.**
- If `coordinated_halt.db` is missing, corrupted, unreadable, or lacks a valid control record on normal component startup → status = UNKNOWN → fail closed. The facade does not create or initialize the file.
- **Explicit authorized bootstrap** is a separate, one-time, operator-invoked operation (not part of normal component constructors or startup). Bootstrap may create the database and insert the initial `NOT_HALTED` row only when:
  - no prior authority file exists,
  - the operator supplies an explicit bootstrap flag or confirmation, and
  - the operation is performed outside the normal request path.
- After a successful bootstrap the file exists and normal startup only opens and reads it.
- Missing control record after a previously valid database, unrecognized state value, or schema version mismatch → UNKNOWN → fail closed.
- Any state value other than `'NOT_HALTED'` or `'HALTED'` → UNKNOWN → fail closed.

### Schema versioning
- `schema_meta.schema_version` is an integer string.
- Readers refuse to operate if the version is higher than the code understands (fail closed).
- Additive migrations only; no automatic data-destroying upgrades.
- Version is checked inside the same transaction as the HALT read/write.

### Monotonic HALT transitions
- Allowed transition: `NOT_HALTED` → `HALTED` (sequence increments by 1).
- Forbidden in this architecture: `HALTED` → `NOT_HALTED` (no clearing method exists).
- Duplicate activation while already `HALTED` is a no-op that still returns success after confirming the committed state; sequence is not incremented.
- Sequence is strictly monotonic and only increases on a successful activation.

### Transaction requirements
- Every read of status and every activation uses `BEGIN IMMEDIATE`.
- Activation updates `halt_state` and inserts a `halt_events` row in the same transaction.
- The facade reports the HALT as durably activated only after the transaction commits successfully.
- Timeout on lock acquisition is finite (recommended 5 s, matching existing component timeouts); timeout → fail closed.

### Startup initialization
- Normal constructor / startup opens an existing database only. It verifies schema version and reads the control record under a transaction.
- It does **not** create the database or insert an initial `NOT_HALTED` row.
- Missing file, unreadable file, missing control record, or any initialization failure → the object is unusable; `require_not_halted()` fails closed.
- Existing components that are given a CoordinatedHalt instance must not fall back to local-only HALT checks once the coordinated path is commissioned.
- Authorized bootstrap (separate entry point) is the only path that may create the initial `NOT_HALTED` authority.

### Idempotent HALT requests
- Calling `activate_halt()` when already `HALTED` succeeds and returns the existing sequence.
- No error is raised for duplicates.
- An audit event of type `duplicate_halt_request` may be written (best-effort).

### Durable commit confirmation
- `activate_halt()` returns only after the `BEGIN IMMEDIATE` … `COMMIT` succeeds.
- If the process crashes before commit, the HALT is not activated (see failure matrix).
- Callers must treat the return of `activate_halt()` as the linearization point for “HALT is durably active.”

### Restart behavior
- On reopen the facade reads the committed `halt_state` row.
- A previously committed `HALTED` remains `HALTED`.
- There is no in-memory cache that can override the database; every `read_status()` / `require_not_halted()` re-reads under a transaction.

### Invalid-state handling
- Missing row, unrecognized state value, schema version mismatch, or any SQLite error during the read → status = UNKNOWN.
- UNKNOWN fails closed (`require_not_halted()` raises; evaluation returns rejected with `execution_enabled=False`).

### Multi-process access
- SQLite WAL mode is recommended for the control database (better concurrent readers).
- Writers use `BEGIN IMMEDIATE` so only one activation can proceed at a time.
- Multiple processes may call `activate_halt()` concurrently; exactly one will increment the sequence; others observe the already-committed HALTED state and return success.
- Readers never block writers indefinitely; lock timeout fails closed.

### NOT_HALTED vs UNKNOWN
- `NOT_HALTED`: control record present, state exactly `'NOT_HALTED'`, schema version understood, read succeeded.
- `UNKNOWN`: any failure to establish the above (missing file, corruption, lock timeout, missing row, bad value, version mismatch).
- UNKNOWN must fail closed. It is never treated as NOT_HALTED.

---

## 4. HALT API (Task 2)

Proposed class: `CoordinatedHalt`

```python
class HaltStatus(str, Enum):
    NOT_HALTED = "NOT_HALTED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"

class HaltError(RuntimeError):
    """Raised when HALT state cannot be established as clear."""

class CoordinatedHalt:
    def __init__(self, database_path: str | Path) -> None: ...

    def activate_halt(self, reason: str) -> int:
        """
        Durably activate HALT.
        Returns the sequence number after the transaction commits.
        Idempotent if already HALTED.
        Raises HaltError if the write cannot be committed.
        Never grants execution permission.
        """

    def read_status(self) -> tuple[HaltStatus, int | None]:
        """
        Returns (status, sequence).
        sequence is None when status is UNKNOWN.
        Never raises for ordinary read failures; maps them to UNKNOWN.
        """

    def require_not_halted(self) -> None:
        """
        Raises HaltError if status is not exactly NOT_HALTED.
        Used at the start of any protected evaluation or acceptance path.
        """
```

### Persistence and failure semantics
- `activate_halt()` is durable only after commit. A return value means the HALT is committed.
- `read_status()` is a point-in-time observation under a short transaction.
- `require_not_halted()` is the enforcement point; it must be called inside the same logical operation that would otherwise accept work, ideally under the component’s own transaction when practical, but always against the shared control database.
- No method returns or sets `execution_enabled=True`.
- Clearing HALT is not part of this API.

---

## 5. Concurrency control (Task 3)

### Sequence that must be addressed
1. Component A reads NOT_HALTED.
2. Process B calls `activate_halt()` and commits.
3. Component A continues and performs a local state transition or evaluation.

### Linearization points
- **HALT activation linearization point:** successful commit of the `activate_halt()` transaction in the shared control database.
- **Protected local transition linearization point:** the moment a component has both (a) observed NOT_HALTED via `require_not_halted()` and (b) begun its own local `BEGIN IMMEDIATE` transaction that will accept or evaluate work. The local transaction must re-check the coordinated flag (or have been started only after a fresh check) before committing any local acceptance.

### SQLite locking on the shared control database
- `BEGIN IMMEDIATE` on the control database serializes writers.
- A reader using `BEGIN DEFERRED` or a short `BEGIN IMMEDIATE` sees a consistent snapshot.
- Lock timeout is finite; failure → UNKNOWN → fail closed.
- This gives a clear happens-before relationship for the HALT flag itself.

### Guarantees that can be achieved for local database operations
- Within a single component’s own database, a local `BEGIN IMMEDIATE` can make the local acceptance atomic with a re-check of the coordinated flag if the re-check is performed inside that transaction (by opening a second connection to the control database or by using a shared connection carefully).
- Restart safety: a committed HALT is visible to every subsequent process.

### Guarantees that cannot be achieved across separate databases
- There is no distributed transaction spanning `coordinated_halt.db`, `policy.db`, and `protocol.db`.
- A shared HALT check does **not** make transactions across these separate databases atomic.
- A component can observe NOT_HALTED, then a HALT can commit, then the component can still commit a local acceptance that was already in flight.
- Challenge consumption in the identity database and policy evaluation remain non-atomic with each other and with the HALT flag.
- A shared HALT flag alone does **not** eliminate concurrency races for in-flight local work.

### Future execution fencing
Before any external side effect (GitHub write, Railway deploy, IFTTT, trading, Hermes action) can be commissioned:
- An execution fence must be able to cancel or refuse the side effect even after local acceptance.
- The fence must re-check the coordinated HALT flag at the last possible moment before the external call.
- Local acceptance must be reversible or must not imply external permission.
- This architecture does not provide that fence; it only provides the durable HALT signal that the fence would consult.

---

## 6. Legacy migration (Task 4)

### Existing stores
- AuthorizationPolicy: `authorization_control` key `'halted'` value `'1'`.
- CollaborationProtocol: `control` key `'halted'` value `'1'`.

### Migration sequence (proposed)
1. On first startup with a CoordinatedHalt instance, the facade (or a one-time migration helper) opens both legacy databases in read-only mode if they exist.
2. If either legacy store contains a committed HALT (`value = '1'`), the coordinated store is activated (`NOT_HALTED` → `HALTED`) if it is not already HALTED. This preserves existing HALT records.
3. If a legacy database exists but cannot be read (corruption, permission, lock timeout), migration fails closed: the coordinated store is set to HALTED (or left UNKNOWN, which also fails closed) and an audit event `migration_failure` is recorded. No component is allowed to proceed as NOT_HALTED.
4. Mixed-version processes: a process still using only the legacy flag must be treated as unsafe. Deployment order must ensure that no process accepts work without consulting the coordinated authority once migration has begun. A process that cannot obtain a CoordinatedHalt instance fails closed.
5. After successful migration the legacy flags are left in place (not cleared) so that any straggler process that still checks them will also see HALT. They are never used as the source of truth again.
6. Migration completion criteria:
   - Coordinated store exists, schema version understood, and state is either NOT_HALTED (only if no legacy HALT was found) or HALTED.
   - Both legacy databases have been successfully read or confirmed absent.
   - An audit event `legacy_halt_discovered` or `migration_complete` has been written.
7. No automatic recovery: migration never clears a HALT. A failed migration leaves the system failed closed.

### Legacy process cutover
Before the coordinated authority is declared authoritative, all legacy-only processes must be stopped or isolated so they can no longer accept work.

Cutover sequence:
1. Drain or stop every process that reads only its original local HALT flag (AuthorizationPolicy or CollaborationProtocol independent stores).
2. Confirm no such process remains able to accept or evaluate work.
3. Perform the migration read of legacy stores and activate the coordinated store if any legacy HALT is present.
4. Deploy only processes that require a CoordinatedHalt instance and call `require_not_halted()` before accepting work.
5. Declare the coordinated authority authoritative only after steps 1–4 succeed.

A new HALT must not be bypassable by older processes that still read only their original databases. Therefore coordinated HALT is not treated as the sole authority until the cutover is complete. During the cutover window any mixed-version detection or inability to reach the coordinated store fails closed.

### Routing of existing entry points
- `AuthorizationPolicy.halt()` → calls `coordinated.activate_halt(reason="policy_halt")` then writes its local audit event. Local flag may also be set for backward compatibility during migration, but readers must prefer the coordinated flag.
- `CollaborationProtocol.halt()` → same pattern.
- After cutover and migration, `is_halted` / `require_not_halted` on both components consult only the coordinated authority.

---

## 7. Failure-handling matrix (Task 5)

| # | Condition | Expected behavior | execution_enabled |
|---|-----------|-------------------|-------------------|
| 1 | Shared database unavailable (missing file, permission, disk error) | Status = UNKNOWN; ordinary startup does **not** recreate as NOT_HALTED; `require_not_halted()` raises; evaluation rejected | False |
| 2 | SQLite lock timeout | Status = UNKNOWN; fail closed | False |
| 3 | Corrupted database | Status = UNKNOWN; fail closed; no automatic repair that could clear HALT | False |
| 4 | Missing control record | Status = UNKNOWN; fail closed | False |
| 5 | Unknown stored state | Status = UNKNOWN; fail closed | False |
| 6 | Process crash before HALT commit | HALT not activated; next reader sees previous committed state | False |
| 7 | Process crash after HALT commit | HALT remains active on restart | False |
| 8 | Concurrent HALT requests | One increments sequence; others observe HALTED and succeed (idempotent) | False |
| 9 | Partial migration | Fail closed; coordinated store HALTED or UNKNOWN; no bypass | False |
| 10 | Stale HALT observation | Next `require_not_halted()` or re-check inside local transaction sees current state; in-flight local work may still complete (see concurrency limitations) | False |
| 11 | Audit-write failure (secondary) | Mandatory HALT state remains committed; secondary audit failure is ignored and never reverses the HALT | False |
| 12 | Component initialized without coordinated authority | Fail closed; no local-only fallback once migration has started | False |

Every case in which operational safety cannot be established fails closed.

---

## 8. Audit semantics (Task 6)

### Safety-critical vs best-effort (audit transaction semantics)
- **Mandatory / safety-critical:** the `halt_state` row update (state + sequence) in the coordinated database. This write is the sole determinant of whether HALT is durably activated. It commits in its own transaction (or is committed before any secondary audit). A failure here means the HALT is not activated.
- **Best-effort secondary:** rows in `halt_events` and any lifecycle audit events written to the policy database. These are written after the HALT state transaction has committed, or in a separate transaction whose failure is caught and ignored.
- A secondary audit failure must **never** reverse, roll back, or erase a committed HALT. Implementation must not place secondary audit writes in the same transaction as the `halt_state` update if a failure of the audit write could cause the HALT state change to roll back.

### Required audit events (best-effort unless noted)
- `halt_requested` — activation attempted.
- `halt_durably_activated` — written in the same transaction as the state change (safety-critical companion).
- `duplicate_halt_request` — already HALTED.
- `halt_read_failure` — status mapped to UNKNOWN.
- `halt_evaluation_rejection` — a component rejected work because of HALT or UNKNOWN.
- `legacy_halt_discovered` — migration found a legacy HALT.
- `migration_failure` — legacy state could not be determined.
- `coordination_failure` — component could not reach the coordinated authority.

### Rules
- A failed auxiliary audit write must not erase a durably committed HALT.
- Audit records must not store signatures, private keys, or sensitive payloads.
- Audit is not claimed to be tamper-proof.

---

## 8.1 Lifecycle audit failure (current verified behavior)

In the current verified code (`AuthorizationLifecycle._audit`):
- The call to `policy.record_lifecycle_audit()` is wrapped in a bare `except Exception: pass`.
- If auxiliary lifecycle audit persistence fails for any reason (disk full, lock, constraint, etc.), the exception is swallowed.
- The evaluation decision is unchanged: the method still returns the `LifecycleResult` that was already determined.
- A failed auxiliary lifecycle audit does not enable execution and does not alter `execution_enabled` (always `False`).

Distinction:
- `LifecycleResult.accepted=True` with `policy_state=PENDING` means synthetic evidence matched a PENDING authorization record. It does **not** grant execution permission, does not transition the authorization to APPROVED, and does not authorize any protected action.
- Execution permission remains disabled. Accepting synthetic evidence and granting execution permission are distinct; only the former can occur in the current offline bridge, and only with `execution_enabled=False`.

This behavior is preserved: auxiliary audit failure must not change the fail-closed evaluation outcome.

---

## 9. Regression-test plan (Task 7)

Tests are specified here; they are **not implemented** in this checkpoint.

| Scenario | Expected result |
|----------|-----------------|
| Normal offline synthetic evaluation | Accepted or rejected per existing rules; `execution_enabled=False`; HALT remains NOT_HALTED |
| Coordinated HALT activation | `activate_halt()` returns sequence; subsequent `read_status()` is HALTED; evaluation rejected |
| HALT through AuthorizationPolicy.halt() | Coordinated store becomes HALTED; policy and protocol both observe it |
| HALT through CollaborationProtocol.halt() | Same |
| HALT persistence across restart | New process sees HALTED |
| Concurrent HALT and evaluation | At least one evaluation may race; after HALT commit all new evaluations rejected; no execution enabled |
| Multiple simultaneous HALT requests | All succeed; sequence increments at most once |
| Multiple process instances | All observe the same committed state |
| Missing or corrupted control database | UNKNOWN; fail closed |
| Database lock contention | Timeout → UNKNOWN → fail closed |
| Legacy HALT preservation | Existing legacy HALT causes coordinated HALTED |
| Partial migration failure | Fail closed; no bypass |
| Audit persistence | Events survive restart |
| Audit-write failure | HALT remains committed |
| No automatic recovery | No API or path clears HALT |
| No execution capability | Every result has `execution_enabled=False` |
| Oversized authorization identifier (PR #12 coverage) | Rejected before any DB query; `execution_enabled=False` |
| Lifecycle audit persistence failure | Evaluation still fails closed; HALT (if any) not erased |

---

## 10. Implementation sequence (future milestones)

1. Introduce `CoordinatedHalt` with the schema and API above (unit-tested in isolation).
2. Add migration helper that reads legacy flags and activates coordinated HALT if needed.
3. Inject `CoordinatedHalt` into AuthorizationPolicy, CollaborationProtocol, and AuthorizationLifecycle.
4. Route existing `halt()` methods to `activate_halt()`.
5. Replace independent flag checks with `require_not_halted()` / `read_status()`.
6. Add the regression tests listed above.
7. Confirm full suite still passes (baseline 470 + new tests).
8. Only after review: consider execution fencing for any future external side-effect path.

This checkpoint performs none of the above.

---

## 11. Commissioning blockers (remaining)

- Coordinated HALT is not yet implemented; independent flags remain.
- Ordinary startup must not recreate a missing authority as NOT_HALTED (authorized bootstrap is separate).
- Legacy-only processes must be stopped or isolated before coordinated HALT is authoritative.
- Cross-database atomicity is not guaranteed.
- No execution fence exists for external side effects.
- Real Chief enrollment and approval capture are not implemented.
- `execution_enabled=False` remains mandatory.
- No automatic HALT reset will ever be added without a separate, explicit Chief-authorized milestone.
- Hermes has no execution privilege.
- Synthetic keys must not authorize real operations.

---

## 12. Separation of verified vs proposed

| Aspect | Verified today (a7e091f) | Proposed (this architecture) |
|--------|--------------------------|------------------------------|
| HALT storage | Two independent SQLite flags | Single coordinated control database |
| Lifecycle check | Sequential reads of both flags (race) | Single `require_not_halted()` |
| Identity HALT | None | Consults coordinated authority via bridge |
| Clearing HALT | None | Still none |
| execution_enabled | Always False | Always False |
| Production code | Unchanged in this commit | Unchanged in this commit |

