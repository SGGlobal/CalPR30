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

**Windows spawn provenance.** The process test uses multiprocessing.get_context("spawn"), which is also the Windows default. The worker _process_contention_worker is a module-level function in this test module. Spawn re-imports the module by name in each child using the parent's sys.path (pytest's tests directory and the configured pythonpath "."), and the child's main-module fixup skips pytest.__main__. The parent pre-initializes one disposable store by constructing the gate once (schema version 2 and WAL). Concurrent fresh-store creation is not claimed. Each child installs the four named zero-invocation sentinels with its own pytest.MonkeyPatch, because parent monkeypatches are not inherited by spawn. It builds its own trust root, signers, gate and SQLite connections and signs its own envelope. Only then does it report its PID and ready status. It waits up to 60 seconds on one shared release event, makes exactly one reserve call, and returns a typed outcome or the full formatted traceback together with its sentinel counts. The parent requires two distinct child PIDs that match Process.pid and differ from its own, sets the release once, collects both results within finite deadlines, and requires exit code 0. A timeout, infrastructure error or child exception fails the test and never counts as the loser. There are no retries. Three fresh-store repetitions run. A finally block terminates any still-live child, falls back to kill, joins with finite timeouts and closes both pipe ends. Children are also daemonic. A cleanup problem fails the test only when no earlier failure is in flight, so it never masks the original failure. Each repetition writes its child evidence (PIDs, outcomes, reasons, sentinel counts, exit codes, winner) to process-contention-evidence.json in its tmp_path.

**Cross-thread diagnostics.** The contender sets an event immediately before it calls simulate(). That shows only that the contender reached the call site while the first transition was held open in the fixture policy; it is not proof that the contender is blocked inside the lock acquire. The bounded 0.3-second check is an observation, not lock-contention proof. The conclusive assertions are the outcomes. Only started threads are joined, and every wait and join is finite. This is same-instance threading in one process and is distinct from the direct-gate process evidence.

**Remaining limits.** Even when every case passes, this establishes only these disposable direct-gate cases and the thread diagnostic above. It does not establish simulator cross-process exclusivity, atomic fresh-store creation, operational recovery, or coverage of every commit seam, and it is not exhaustive scheduling or crash proof. These constraints remain open:
- concurrent invalidate_for_restart can race an in-flight simulate() call;
- the HALT probe-to-commit race;
- the schema-to-WAL (I3/I4) non-atomic initialization gap;
- synthetic trust and evidence only, with no real Chief or SKIPPY authorization;
- no cross-database atomicity;
- store-binding assumptions (the exists()/wal/shm freshness check is not an exclusive creation lock);
- ledger lifecycle observation (this phase performs zero ledger appends);
- the §G blocker.

## Tests, limitations and next approval

Run the two new pytest modules against the real QLA source and repository dependencies before any integration decision. Cover approved and correction flows, invalid order/SHA/evidence/role/verdict, duplicate identity, expired/revoked signatures, HALT unavailable, bad SQLite state, restart denial, concurrency and a zero-protected-ledger-call assertion. Report the exact Git SHA and test results.

The local preparation environment can test the pure contract directly; integration with a compatible local stand-in is **not** equivalent to testing the authentic B2.1 gate. Independent repository CI remains mandatory before integration.

**Stop gates:** Do not modify authenticated_envelope.py, offline_authenticated_adapter.py, offline_signature_verifier.py, offline_admission_gate.py, checkpoint_ledger.py, existing tests, workflows, credentials, deployment, or external transport to make this feature work. Any protected-file change requires a separate security review and explicit Chief approval. Do not create or merge a PR, dispatch workflows, deploy, operate unattended agents, connect live transport, or trade under this checkpoint.
