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

Competitor tests run in one process: one test uses one simulator instance and its lock, the other uses two gate instances with separate SQLite connections. Neither is cross-process seam proof, and true cross-process exclusion remains unproven. Lock and corruption tests hold a real write lock from a separate connection, or damage a checkpointed disposable store file, and rely on the gate's unchanged default busy wait. They show fail-closed errors and session invalidation, not operational recovery.

**T25 single-stream exclusion is not durable-namespace proof.** The simulator models exactly one project/task/checkpoint/assignment stream. A message with a different checkpoint_id is rejected by the contract's stream-binding check before any durable identity lookup, so a session cannot continue across checkpoints. That is a simulator-scope exclusion. It is **not** evidence that the B2.1 gate's durable project+task identity namespace rejects message_id or idempotency_key reuse across checkpoints. This work order adds no test of that durable cross-checkpoint conflict, and the gap remains open.

The store-creation and concurrent-invalidation limits in the failure and restart model remain open constraints, not solved: the exists()/wal/shm freshness check is not atomic cross-process exclusive creation, and invalidate_for_restart can race an in-flight simulate() call.

## Tests, limitations and next approval

Run the two new pytest modules against the real QLA source and repository dependencies before any integration decision. Cover approved and correction flows, invalid order/SHA/evidence/role/verdict, duplicate identity, expired/revoked signatures, HALT unavailable, bad SQLite state, restart denial, concurrency and a zero-protected-ledger-call assertion. Report the exact Git SHA and test results.

The local preparation environment can test the pure contract directly; integration with a compatible local stand-in is **not** equivalent to testing the authentic B2.1 gate. Independent repository CI remains mandatory before integration.

**Stop gates:** Do not modify authenticated_envelope.py, offline_authenticated_adapter.py, offline_signature_verifier.py, offline_admission_gate.py, checkpoint_ledger.py, existing tests, workflows, credentials, deployment, or external transport to make this feature work. Any protected-file change requires a separate security review and explicit Chief approval. Do not create or merge a PR, dispatch workflows, deploy, operate unattended agents, connect live transport, or trade under this checkpoint.
