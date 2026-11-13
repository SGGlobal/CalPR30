# BRIDGE-02B.2.2 — Synthetic Compatibility Fixture Contract

**Status:** Bounded offline feature branch. Independent verification against the existing repository and GitHub CI remain pending. Never deploy this simulator. Execution, external transport, and trading remain disabled.

## Scope and provenance

Exactly five new files are authorized: offline_bridge02b2_contract.py, offline_bridge02b2_simulator.py, tests/test_offline_bridge02b2_contract.py, tests/test_offline_bridge02b2_simulator.py, and this document.

Reference source: PR #26 head 8176510250a23d5524919a04323a57738532c8d8. Its pre-existing CI passed 992 tests. This is baseline evidence, **not** test evidence for this feature branch.

The older AuthenticatedEnvelope is checksum-only and permits the sha alias and correction_request. B2.1 accepts a separate cryptographically signed SignedEnvelope, prohibits the sha alias and reviewed_sha field, and permits only assignment, implementation, correction, and review. Existing signed legacy messages must never be translated or re-signed as an authority upgrade.

## Closed synthetic input contract

SyntheticInput includes sender, role, recipient, project/task/checkpoint/assignment/message/idempotency IDs, a positive integer sequence, UTC timestamps, and a plain scalar-only payload.

| Event | Exact payload keys | Actor/role to recipient |
| --- | --- | --- |
| assignment | type, objective, evidence_id, base_sha | chief/chief to jason |
| implementation | type, evidence_id, implementation_sha | jason/implementer to skippy |
| correction | type, evidence_id, implementation_sha | jason/implementer to skippy |
| review | type, evidence_id, implementation_sha, verdict | skippy/reviewer to jason |

The objective and evidence_id must be nonempty and have no surrounding whitespace. Bound SHA fields require exactly 40 lowercase hexadecimal characters. The only review verdicts are approved and changes_requested. All extra keys and unknown types are denied. SHA formatting does not prove commit existence or independent review.

The simulator constructs **new** SignedEnvelope test messages from copied typed inputs. It uses B1 canonical bytes and Ed25519 signing keys with mandatory test-b2b2- key ID prefixes. The prefix is a segregation convention, not operational key attestation. Fixture signatures do not authenticate real agents or Chief authorization.

## Deterministic workflow table

| Current in-memory state | Event and preconditions | Next state |
| --- | --- | --- |
| EMPTY | assignment, sequence 1 | AWAIT_IMPLEMENTATION |
| AWAIT_IMPLEMENTATION | implementation, unused SHA | AWAIT_REVIEW |
| AWAIT_REVIEW | review approved, latest SHA | CLOSED |
| AWAIT_REVIEW | review changes_requested, latest SHA | AWAIT_CORRECTION |
| AWAIT_CORRECTION | correction, unused SHA | AWAIT_REVIEW |
| CLOSED or disallowed transition | anything | DENY |

This harness simulates exactly one project/task/checkpoint/assignment stream. Each reserved transition consumes one sequence; exact redeliveries are only duplicate *candidates* and still require current B2.1 signature, HALT, evidence and durable identity checks. Identical duplicates never advance the in-memory workflow; conflicting IDs fail. In B2.1, the durable identity namespace is project+task, including reuse across checkpoints.

The correction_request event is not silently accepted or relabeled. A *new test-only* review with verdict changes_requested models that workflow step; no legacy signed message is converted.

## Explicit test-only authorities

SyntheticBridgeSimulator requires an injected matching test-only TrustRoot and signing-key map, a contextual test fixture policy, an evidence verifier, and an injected HALT provider. No default acceptance is permitted.

The existing B2.1 EvidenceVerifier interface accepts only kind, assignment_id, bound_sha, evidence_id, and now. It does not authenticate project/task/action/sender/role as a production proof. An injected string of verified is a synthetic test result, **not** real Chief or SKIPPY authorization.

The fixture policy is given a separate scalar-only copy of the validated input, never the internal plan payload object. Callback-visible field or payload changes are detected and rejected before signing or gate reservation. next_snapshot and the receipt fingerprint stay bound to those unchanged internally validated fields. Altered policy input is never authorized and is never used to recompute a plan.

B2.1 OfflineAdmissionGate.reserve verifies freshly signed messages, the sender matrix, evidence, SHA and sequence, and persists RESERVED only. RESERVED is **not** ACCEPTED, ledger appended, dispatched, deployed, or traded. The simulator must never import or call OfflineAuthenticatedAdapter.accept, protected ledger writers, network transports, or order execution.

## Failure and restart model

The workflow tracker is in-memory only. Simulated process restart invalidates it. Creating a new simulator requires a fresh disposable SQLite database; existing state is not resumed as authority. The pre-construction exists()/wal/shm freshness check is not an atomic cross-process exclusive store-creation lock and must not be treated as cross-process exclusivity. invalidate_for_restart does not acquire the simulator lock, so a concurrent invalidation can race an in-flight simulate() call; that is not operational recovery or crash-safe session control. Gate errors, including indeterminate commit, invalidate the session without replay. Existing PENDING or UNCERTAIN reservations deny B2.1 initialization; they are not evidence that a ledger write was absent.

This phase performs **zero ledger appends**. Real post-ledger-append crashes, cross-store reconciliation, manual recovery and authoritative HALT/ledger atomicity must be studied in a different, separately authorized security milestone. The injected HALT probe has an unresolved probe-to-commit race. SQLite reservations are not a tamper-evident audit ledger.

## Evidence layers and explicit coverage boundaries (work order 6845134418)

The two new test modules keep three evidence layers separate. Pure-contract tests call plan_transition only and perform no I/O. Simulator tests drive SyntheticBridgeSimulator over the authentic B2.1 OfflineAdmissionGate on a disposable tmp_path SQLite store. Direct-gate tests call the authentic OfflineAdmissionGate and B1 verify_signature with test-signed SignedEnvelope values built from the verifier's own canonical encoders, with no simulator. A pass at one layer is not claimed as evidence for another.

Explicit cases now include: an authentic AuthenticatedEnvelope built by build_envelope (rejected by the contract, by the simulator before any gate call, and by the gate as a malformed payload); an absent required SHA kept distinct from a None value; absent verdict and absent evidence; wrong sender and wrong recipient; signed payload tamper, signed field tamper, future timestamp, unknown key and wrong signing key; duplicate re-authentication after HALT, evidence and trust-root changes made after reservation; distinct same-sequence competitors with finite waits; and real SQLite write-lock and file-corruption failures. The review verdict is closed by the synthetic contract; the B2.1 gate does not inspect it, so absent-verdict evidence exists at the contract and simulator layers only. The trust-root change is a test substitution of the gate's injected trust_root attribute with an authentic load_trust_root variant; it is not a production key-rotation mechanism.

Every simulator-module test runs with named zero-invocation runtime sentinels on CheckpointLedger.record_event, ProductionHTTPTransport.__call__, urllib.request.urlopen and polymarket.orders.place_limit_order, asserted at teardown; both complete workflows also assert them inline. The sentinels observe those named attributes only, not every possible side channel. The AST import check remains supplementary.

Competitor tests run in one process: one test uses one simulator instance and its lock, the other uses two gate instances with separate SQLite connections. Neither is cross-process seam proof. Separate-process direct-gate contention evidence was added later by work order 6846218272 (see below); it does not make the simulator cross-process exclusive. Lock and corruption tests hold a real write lock from a separate connection, or damage a checkpointed disposable store file, and rely on the gate's unchanged default busy wait. They show fail-closed errors and session invalidation, not operational recovery.

**T25 single-stream exclusion is not durable-namespace proof.** The simulator models exactly one project/task/checkpoint/assignment stream. A message with a different checkpoint_id is rejected by the contract's stream-binding check before any durable identity lookup, so a session cannot continue across checkpoints. That is a simulator-scope exclusion. It is **not** evidence that the B2.1 gate's durable project+task identity namespace rejects message_id or idempotency_key reuse across checkpoints. Work order 6845134418 added no test of that durable cross-checkpoint conflict. Direct-gate evidence for it was added later by work order 6846218272 (see below); that evidence does not change this simulator-scope statement.

The store-creation and concurrent-invalidation limits in the failure and restart model remain open constraints, not solved: the exists()/wal/shm freshness check is not atomic cross-process exclusive creation, and invalidate_for_restart can race an in-flight simulate() call.

## Single active transition and reentrancy (work order 6845727928)

Finding 6845540923: simulate() holds a reentrant RLock while it calls the caller-supplied fixture policy, and the gate calls the injected HALT and evidence fixtures during reserve. A callback holding the simulator could re-enter simulate() on the same thread. At e54ca6e a one-shot recursive policy made the inner call reserve and advance the snapshot, after which the outer call (planned from the earlier snapshot) reached the gate, received an unexpected duplicate, raised AdmissionError and invalidated the session.

Contract: one session has at most one active transition. simulate() checks and sets an in-progress flag while holding the existing RLock and clears it in a finally block on every return and exception path. A nested call on the same thread is rejected with the deterministic reason "reentrant simulate rejected" before planning, signing or gate access, and it changes no snapshot or reservation row. The RLock is kept: a plain non-reentrant lock would deadlock same-thread recursion. Another thread never sees the flag set, because the flag is only set and cleared while the lock is held; such a call waits for the lock and runs afterwards (normal cross-thread serialization). Gate exceptions still invalidate the session without retry, and the flag is cleared afterwards.

Outer continuation: the outer call continues after a rejected inner attempt and is decided by its own plan, policy result and gate result. Because the rejected inner attempt changed nothing, the outer plan is still based on the current snapshot, and a valid outer call commits exactly one reservation row whose receipt fingerprint matches the outer input. Separately, the simulator records the snapshot it planned from and refuses to proceed if that snapshot changed during the policy call (reject before signing) or during reserve (raise AdmissionError and never apply the stale plan); both invalidate the session.

Limits: this is a test-fixture consistency control, not a sandbox. In-process code can bypass it, for example by calling the private _simulate_locked method; the snapshot-change checks then fail closed, but a reservation made by the bypassing call may already be committed. The guard adds nothing for cross-process exclusion, durable T25 cross-checkpoint identity, store-creation freshness or concurrent invalidate_for_restart races; those constraints remain open as stated above. The direct-gate T25 and separate-process evidence of work order 6846218272 is independent of this guard.

## Durable identity namespace and process contention evidence (work order 6846218272)

This is a bounded acceptance-evidence checkpoint, not full harness acceptance or commissioning. It changes no production simulator, contract or protected source. All cases below are in tests/test_offline_bridge02b2_simulator.py and use disposable pytest tmp_path stores.

| Case (pytest id) | Layer | Asserted result |
| --- | --- | --- |
| test_t25_direct_gate_cross_checkpoint_identity_reuse_rejected[a_reused_message_id_new_idempotency_key] | Direct gate, one process | rejected, reason "conflicting identity"; never duplicate; complete rows unchanged |
| test_t25_direct_gate_cross_checkpoint_identity_reuse_rejected[b_new_message_id_reused_idempotency_key] | Direct gate, one process | rejected, "conflicting identity"; never duplicate; complete rows unchanged |
| test_t25_direct_gate_cross_checkpoint_identity_reuse_rejected[c_both_reused_same_row] | Direct gate, one process | rejected, "conflicting identity"; never duplicate; complete rows unchanged |
| test_t25_direct_gate_cross_checkpoint_identity_reuse_rejected[d_cross_matched_a1_message_a2_key] and [d_cross_matched_a2_message_a1_key] | Direct gate, one process | rejected, "ambiguous or conflicting identity matches"; never duplicate; complete rows unchanged |
| Fresh-ID checkpoint-B control (inside each of the five cases above, after the conflict) | Direct gate, one process | reserved, "ok"; checkpoint-A rows unchanged; exactly one new checkpoint-B sequence-1 row |
| test_t25_direct_gate_identity_namespace_is_project_and_task[other_project] and [other_task] | Direct gate, one process | the reused checkpoint-A identity reserves in another project or task; exact redelivery there is "duplicate"; checkpoint-A rows unchanged |
| test_separate_process_contention_one_reserved_one_sequence_rejected[0], [1] and [2] | Direct gate, two spawned processes, one pre-initialized store | one child reserved "ok", the other rejected "sequence 1 != 2"; exactly one committed row equal to the winner; zero sentinel counts in each child |
| test_cross_thread_call_waits_for_active_transition_not_rejected | Simulator, two threads, one instance, one process | both reserved; contender never rejected as reentrant |

**T25 direct-gate setup.** Checkpoint A (an assignment at sequence 1 and an implementation at sequence 2, project QLA, task task-test) is reserved through the authentic OfflineAdmissionGate. A new gate instance is then constructed against the same store, which runs the gate's read-only preflight and locked revalidation of the existing schema. Each conflict is an otherwise valid, freshly test-signed checkpoint-B sequence-1 assignment in the same project and task. Its signed payload equals checkpoint-A row 1's payload, so case (c) differs from that row only in checkpoint_id. Every column of every row, including id, payload_digest and recorded_at, is compared before and after. The fresh-ID control uses the same reconstructed gate, store, project, task, checkpoint, sequence and payload, which shows that checkpoint mismatch, sequence, signature, evidence or HALT did not cause the rejection. The independent project and task controls show the namespace boundary is project+task. This is direct-gate evidence only. It is separate from the simulator's single-stream checkpoint exclusion and is not simulator evidence. The expectations are asserted strictly; no case is marked xfail.

**Windows spawn provenance.** The process test uses multiprocessing.get_context("spawn"), which is also the Windows default. The worker _process_contention_worker is a module-level function in this test module. Spawn re-imports the module by name in each child using the parent's sys.path (pytest's tests directory and the configured pythonpath "."), and the child's main-module fixup skips pytest.__main__. The parent pre-initializes one disposable store by constructing the gate once (schema version 2 and WAL). Concurrent fresh-store creation is not claimed. Each child installs the four named zero-invocation sentinels with its own pytest.MonkeyPatch, because parent monkeypatches are not inherited by spawn. It builds its own trust root, signers, gate and SQLite connections and signs its own envelope. Only then does it report its PID and ready status. It waits up to 60 seconds on one shared release event, makes exactly one reserve call, and returns a typed outcome or the full formatted traceback together with its sentinel counts. The parent requires two distinct child PIDs that match Process.pid and differ from its own, sets the release once, collects both results within finite deadlines, and requires exit code 0. A timeout, infrastructure error or child exception fails the test and never counts as the loser. There are no retries. Three fresh-store repetitions run. A finally block terminates any still-live child, falls back to kill, joins with finite timeouts and closes both pipe ends. Children are also daemonic. The leftover check (a child that needed forced termination or is still alive after the finite joins) fails the test only when no earlier failure is in flight, so that particular check does not replace an earlier failure. Cleanup is not guaranteed to be non-masking: an exception raised inside the finally block by the OS-level operations it performs (Process.is_alive, terminate, kill or join, or closing a pipe end) would propagate from the finally block and become the reported error, with the earlier failure kept only as chained exception context. The current tests do not inject such terminate, kill, join or close faults, so this masking path is unexercised (corrected by work order 6846823630). Each repetition writes its child evidence (PIDs, outcomes, reasons, sentinel counts, exit codes, winner) to process-contention-evidence.json in its tmp_path.

**Cross-thread diagnostics.** The contender sets an event immediately before it calls simulate(). That shows only that the contender reached the call site while the first transition was held open in the fixture policy; it is not proof that the contender is blocked inside the lock acquire. The bounded 0.3-second check is an observation, not lock-contention proof. The conclusive assertions are the outcomes. Only started threads are joined, and every wait and join is finite. This is same-instance threading in one process and is distinct from the direct-gate process evidence.

**Remaining limits.** Even when every case passes, this establishes only these disposable direct-gate cases and the thread diagnostic above. It does not establish simulator cross-process exclusivity, atomic fresh-store creation, operational recovery, or coverage of every commit seam, and it is not exhaustive scheduling or crash proof. These constraints remain open:
- concurrent invalidate_for_restart can race an in-flight simulate() call (characterized, not fixed, by work order 6846823630);
- the HALT probe-to-commit race;
- the schema-to-WAL (I3/I4) non-atomic initialization gap;
- synthetic trust and evidence only, with no real Chief or SKIPPY authorization;
- no cross-database atomicity;
- store-binding assumptions (the exists()/wal/shm freshness check is not an exclusive creation lock);
- ledger lifecycle observation (this phase performs zero ledger appends);
- the §G blocker.

## Concurrent invalidation: characterization and serialized-close design (work order 6846823630)

This checkpoint adds characterization tests of CURRENT behavior and a design proposal only. It changes no simulator, contract or protected source: invalidate_for_restart() and simulate() are unchanged, and finding 6846379683 remains **unfixed**. Nothing in this section is implemented.

### Observed current behavior (characterization, not safety)

At this source invalidate_for_restart() writes the invalidation flag without acquiring the simulator lock and returns None. simulate() reads the flag once, at the start of _simulate_locked, before planning. An invalidation that lands after that read does not stop, cancel or roll back the active transition. A passing characterization test proves only that the interleaving reproduces; it is not a safety property, a fix or the proposed semantics below. The expectations were first observed on authentic Windows with a separate probe and then asserted unchanged.

| Case (pytest id) | Point where the callback pauses or invalidates | Calls made when invalidate returns (plan/sign/reserve) | Observed and asserted current result |
| --- | --- | --- | --- |
| test_characterization_cross_thread_invalidate_during_paused_callback_does_not_stop_active_call[policy] | (a) fixture policy, pre-sign; another thread invalidates | 1/0/0; zero reservation rows | invalidate returns None and sets the flag while the call is active (_in_progress True); after release the active call signs and reserves "synthetic reservation only; no ledger append" |
| test_characterization_cross_thread_invalidate_during_paused_callback_does_not_stop_active_call[halt] and [evidence] | (b) authentic gate HALT probe or evidence verifier callback, in-reserve (after signing, before the gate's locked BEGIN IMMEDIATE/INSERT); another thread invalidates | 1/1/1; zero reservation rows | same: the gate completes the insert after invalidation |
| test_characterization_same_thread_callback_invalidate_does_not_stop_active_call[policy] | (c) one-shot fixture policy, same thread, pre-sign | 1/0/0 | invalidate returns None and sets the flag; the active call still signs and reserves |
| test_characterization_same_thread_callback_invalidate_does_not_stop_active_call[halt] and [evidence] | (d) one-shot authentic gate HALT or evidence callback, same thread, in-reserve | 1/1/1 | same |

In all six cases the active call ends with plan/sign/reserve counts 1/1/1, exactly one reservation row whose every column equals the row of a separate never-invalidated reference store (message msg-1, idempotency idem-1, sequence 1, RESERVED, recorded_at equal to NOW), and a snapshot equal to the reference snapshot (AWAIT_IMPLEMENTATION). The flag stays set and _in_progress is False afterwards. The next public simulate() is rejected "simulation invalidated; no crash/restart continuation" before planning, signing or reserve: counts stay 1/1/1 and rows and snapshot are unchanged. These observations match the expectation in finding 6846379683; no discrepancy was observed. Current meaning: invalidate_for_restart() blocks only public calls that read the flag later. It does not wait for, stop or roll back an active transition.

Coordination: each cross-thread case pauses the first callback invocation on a finite Event (10 s), starts the invalidating thread only after the pause is observed, waits for that thread to signal that invalidate_for_restart() returned, records counts, full rows and _in_progress while the worker is still paused, and only then releases. No ordering relies on sleep; only started threads are joined, with finite joins; worker exceptions are captured as formatted tracebacks and asserted empty. The plan/sign/reserve counters wrap and still call the real plan_transition, signing and authentic gate reserve, so the gate's reservation logic runs unchanged. The four named zero-invocation sentinels run for every case (autouse teardown plus an inline assertion). Each case writes its observation to characterization-<mode>-<point>.json in its tmp_path.

Limits: one interleaving per pause point and one message (assignment, sequence 1). Windows between the initial flag read and the policy call, and between the gate commit and the snapshot update, are not separately paused. This is not exhaustive scheduling proof.

### Design proposal: serialized session close (preferred, NOT implemented)

Scope: this synthetic, non-resumable harness only. The proposal serializes close with active transitions; it does not cancel them.

**State.** One terminal "closed" state, entered by an explicit close or by the existing internal fail-stop invalidations (gate exception, snapshot change, unexpected outcome). The flag is read and written only while holding the existing simulator lock. The existing _in_progress flag is reused.

**External-thread close.** A new method close_session(timeout=None) -> CloseResult acquires the existing transition lock. Because an active transition holds that lock for its whole duration (planning, policy, signing and the full gate reserve including HALT and evidence callbacks), the close waits for any active transition to finish by return or exception. Under the lock it sets the closed flag and returns CloseResult("closed"), or CloseResult("already_closed") if the session was already closed or internally invalidated.

**Linearization point.** The write of the closed flag while holding the lock. Every transition that acquired the lock before that point has completed, including any reservation row it committed, before close_session returns. Every public simulate() that acquires the lock after that point reads the flag first and is denied before planning, signing or gate access, with zero new rows.

**Return meaning.** A successful return ("closed" or "already_closed") means: no transition is active on this session; every earlier transition has finished; every future public simulate() call is denied. It does **not** mean that no reservation was written after the close was requested: a transition that acquired the lock first, including one that started after the close was requested, may commit while the close waits.

**Explicit non-goals.** Not cancellation, not immediate HALT, not a bounded-latency return (the close waits as long as the active transition, including slow callbacks), not rollback of committed rows, not crash recovery. No fairness claim: RLock acquisition order is not FIFO, and the close does not exclude calls that acquire the lock first. No in-flight cancellation design across the frozen B2.1 gate or SQLite boundaries: the HALT/evidence callbacks and BEGIN IMMEDIATE/INSERT/commit run inside protected gate code, and interrupting them could leave an indeterminate commit.

**Same-thread callback close.** A close called on the thread that is executing the active transition (from the fixture policy, or from a HALT or evidence callback inside reserve) re-acquires the RLock immediately, because an RLock is reentrant. A locked close therefore cannot by itself refuse or wait; it would set the flag mid-transition, which is today's race. RLock alone cannot refuse same-thread close. Proposed rule: under the lock, if _in_progress is True the caller is necessarily the active thread (another thread cannot hold the lock during a transition), so close_session returns CloseResult("refused_active_transition", "close refused while a transition is active on this thread") deterministically, does not change the closed flag, and leaves the outer transition unchanged: same plan, policy result, gate call, rows and snapshot as a run without the close. A returned result, not an exception, is proposed because the simulator and gate convert callback exceptions into different outer outcomes ("synthetic signing/policy failed closed", "HALT probe failed closed", "evidence verification failed closed"), which would change the outer transition.

**Compatibility impact.** invalidate_for_restart() currently returns None and never blocks. The only in-repository caller is test_storage_exception_invalidates_fixture_and_denies_restart, which calls it on an idle session and ignores the return value. Proposed migration: add close_session(); make invalidate_for_restart() delegate to it and return the CloseResult (return type changes from None to CloseResult). Callers that ignore the return keep working on idle sessions, but the method would then block while another thread's transition is active, and a same-thread call would be refused and must be checked; a None-returning alias would hide the refusal and is rejected. The six characterization tests of this work order are expected to flip and must be replaced deliberately in any implementation order, not silently.

**Alternative: deferred close.** A same-thread close records a pending request and returns CloseResult("deferred"); the outer transition completes unchanged and the finally block of simulate() applies the close under the lock on every return and exception path. Compared with refusal: the caller is not refused, but the return value cannot mean "closed"; it adds a third pending state; the outer transition still commits exactly as with refusal; and error paths must guarantee the deferred close is applied. Refusal is preferred as simpler and deterministic; deferred close remains an option if callers need fire-and-forget behavior.

**Specified edge behavior.**
- Repeated close (sequential): the first returns "closed"; every later call returns "already_closed", raises nothing and changes nothing.
- Duplicate or competing closes from several threads: serialized by the lock; exactly one returns "closed" and the others "already_closed", in unspecified order.
- Close racing a new simulate(): whichever acquires the lock first runs first; if the close linearizes first, the simulate is denied with zero plan/sign/reserve calls. No ordering is promised.
- Policy denial, policy exception or gate rejection in the active transition: the close waits for that outcome, which is unchanged, then returns "closed".
- Gate exception in the active transition: existing internal fail-stop invalidation applies first; the waiting close then returns "already_closed".
- Same-thread close from a callback whose transition later fails: refusal does not alter that failure.
- Nested simulate() from a callback after a refused close: still rejected as reentrant (unchanged).
- Stuck callback: with timeout=None the external close blocks as long as the callback; with a finite timeout it returns CloseResult("timeout_active_transition") and the session stays OPEN (not closed, not cancelled); a later close can succeed after the transition ends. A permanently stuck callback leaves both stuck; terminating the process is a crash outside this model.

**Acceptance tests for a future, separately authorized implementation order.**
1. External close during a paused fixture policy: the close thread has not returned while the policy is paused (finite observation, not latency proof); after release the active call's result and its one row are recorded; the close returns "closed"; the next simulate() is denied with zero plan/sign/reserve calls and no new row.
2. The same with the pause in the authentic gate HALT callback and in the evidence callback.
3. Same-thread close from policy, HALT and evidence callbacks returns "refused_active_transition"; the flag is unchanged during and after; outer outcome, full rows and snapshot equal a reference run without the close; the session stays open for the next call.
4. Repeated close: "closed" then "already_closed" several times, no exception.
5. Competing closes: several threads released together by a barrier; exactly one "closed".
6. Close after a gate exception: "already_closed"; simulate denied.
7. Close waiting on a transition that ends in policy denial, policy exception or gate rejection: unchanged outer outcome, then "closed".
8. Stuck callback with a finite close timeout: "timeout_active_transition", session open; after release the transition completes and a second close returns "closed".
9. Close racing simulate(): assert only the two permitted orderings, with no fairness assertion.
10. Replace the six characterization tests of work order 6846823630 deliberately; keep finite waits, captured worker exceptions, started-thread joins, the named sentinels and the real gate; run the focused and full suites on authentic Windows.

**Open constraints unchanged by this work order.** The concurrent-invalidation finding itself remains unfixed; also open are the HALT probe-to-commit race, the schema-to-WAL (I3/I4) gap, synthetic trust and evidence only, no cross-database atomicity, store freshness and binding (the exists()/wal/shm check is not an exclusive creation lock), ledger lifecycle observation (zero ledger appends) and the §G blocker. execution_enabled=False.

## Tests, limitations and next approval

Run the two new pytest modules against the real QLA source and repository dependencies before any integration decision. Cover approved and correction flows, invalid order/SHA/evidence/role/verdict, duplicate identity, expired/revoked signatures, HALT unavailable, bad SQLite state, restart denial, concurrency and a zero-protected-ledger-call assertion. Report the exact Git SHA and test results.

The local preparation environment can test the pure contract directly; integration with a compatible local stand-in is **not** equivalent to testing the authentic B2.1 gate. Independent repository CI remains mandatory before integration.

**Stop gates:** Do not modify authenticated_envelope.py, offline_authenticated_adapter.py, offline_signature_verifier.py, offline_admission_gate.py, checkpoint_ledger.py, existing tests, workflows, credentials, deployment, or external transport to make this feature work. Any protected-file change requires a separate security review and explicit Chief approval. Do not create or merge a PR, dispatch workflows, deploy, operate unattended agents, connect live transport, or trade under this checkpoint.
