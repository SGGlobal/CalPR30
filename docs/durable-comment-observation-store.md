# Offline durable comment observation store

Baseline: accepted reader `aac231f4ef606c6e5ccc85f1dabbb2ff7c48327e`.
Chief authorized the three-file offline implementation on November 15, 2026
(America/Chicago), following `QLA_Durable_Observation_Storage_Plan.md`.
Adds only the store module, its tests and this contract. All 158 materialized
baseline source/config/document blobs remain unchanged. No polling coordinator,
transport wiring or operational storage is added.

## Explicit APIs

| API | Contract |
| --- | --- |
| initialize_store(path, store_id) | Create-only, private-mode file and new schema; refuse any existing path |
| reserve_attempt(path, store_id, attempt_id, repository, thread, start_token=None) | Confirm PENDING in one transaction before a caller invokes a fake session |
| record_result(path, store_id, attempt_id, result) | Validate and atomically persist the entire tuple/evidence/hint or a body-free HELD result |
| inspect_store(path, store_id) | Inspect all attempt evidence; no reserve/finalize/recovery work |
| inspect_attempt(path, store_id, attempt_id) | Inspect one attempt without releasing its bodies |
| read_observations(path, store_id, attempt_id) | Explicit untrusted-data read for a consistent RECORDED attempt only |

Paths are exact strings, absolute and canonical, with an existing parent. Final
database files must be regular files with one hard link; symlink/aliased paths
are refused. Initialization uses exclusive creation, mode 0600, and never deletes
an uncertain partial file. Inspection uses SQLite URI mode=ro and query_only=ON;
opening a missing path cannot create it. Writers use URI mode=rw and cannot
silently create a replacement. These are caller-trusted disposable paths during
this slice, not a defense against a privileged actor swapping files concurrently.

Store and attempt IDs are exact case-sensitive ASCII alphanumeric/underscore/
hyphen strings, length 1..64. Repository must be the exact SGGlobal/QLA string;
thread must be exact integer 23 or 26. Optional hints require the accepted
canonical named-path, same-thread, per_page=25&page=N grammar, pages 1..9999.
Numeric/reordered caller hints are not silently normalized.

All access requires execution_enabled=False and SQLite >=3.51.3. The version
floor includes the upstream WAL-reset fix; supported older backports are not part
of the initial allow policy. No dependency or runtime-container changes occur.
An older reviewer runtime must report BLOCKED for execution, not skip the guard.

## State and acknowledgment semantics

Reservation uses BEGIN IMMEDIATE and commits a PENDING row with immutable request
identity: attempt ID, repository, thread, initial hint and pinned reader SHA.
Only a normal RESERVED receipt confirms that reservation to its caller. The
store never invokes a reader itself. A duplicate ID never grants another read,
and any unresolved PENDING or HELD attempt blocks a new reservation store-wide.
SQLite's write transaction serializes competing reservation checks/inserts.

Successful NO_NEXT_HINT/BUDGET_STOPPED results become RECORDED in one transaction
containing all observations, canonical evidence, the optional hint and consistency
digest. Failed/refused/conflicting/interrupted results become HELD with metadata
only and no body rows. No per-observation partial commit or callback exists.

| Return or inspected state | Meaning |
| --- | --- |
| INITIALIZED / RESERVED | This invocation received a normal local commit acknowledgment |
| RECORDED / HELD receipt | This invocation received a normal recording commit acknowledgment; HELD is not successful reading |
| PENDING inspection | Unresolved reservation; no automatic retry or completion |
| HELD inspection | Failed session recorded; no automatic clearing or new attempt |
| RECORDED_ON_DISK inspection | Complete consistent record is present in this read transaction; earlier caller acknowledgment is unknown |
| ALREADY_RECORDED receipt | Exact matching record was found through the read-only replay path; no read, write or earlier acknowledgment is inferred |

An identical later recording call compares canonical evidence, the full tagged
body bytes/ordinals/IDs and the batch digest; it returns ALREADY_RECORDED without
opening a write connection. A conflicting replay refuses. Concurrent recording
calls that initially see PENDING still recheck state under the write lock; a
caller losing that race may be refused rather than being granted a second write.
No fresh read permission is created by recording/replaying/inspecting a result.

StoreRefusal has static text for invalid input, unsupported state or detected
inconsistency. StoreUncertain reports storage/commit/close uncertainty and stops
that process's writer for the canonical path. In particular, commit-landed-then-
raised must never return an ordinary recorded receipt. A separate inspection may
find a full RECORDED_ON_DISK record; that does not retroactively acknowledge it.
Read-only exact replay remains possible while that process's writer is stopped.
The inspection blocked flag describes persisted PENDING/HELD records; it is not
a permission grant and does not report the separate process-local writer stop.

Connections are owned by each API invocation and closed in finally paths.
Closing uncommitted work rolls it back; no automatic application retry occurs.
A close failure is uncertainty for a writer, not a claimed success. Inspection
does not reserve, commit, clear rows or follow a saved hint.

## Schema and custody

Three STRICT ordinary rowid tables:

- store_meta: fixed singleton/version, caller store identity, repository/thread
  policy and the utf8-surrogatepass-v1 codec identifier.
- attempts: immutable request identity/digest, PENDING/RECORDED/HELD state,
  canonical evidence BLOB and batch digest. The initial hint is request context;
  a resulting next hint is inside the evidence saved in the same transaction.
- observations: attempt foreign key, ordinal, positive comment ID, null/string
  tag, body BLOB and body digest. Both ordinal and comment ID are unique within
  an attempt. Comment IDs are additionally restricted to SQLite signed positive
  64-bit integers; larger IDs accepted by the in-memory reader are refused here,
  never truncated or coerced.

The exact schema SQL, object inventory, table flags, unique-index origins/key
columns/order/collations, integrity check, foreign keys and row contents are
validated. Unexpected objects/triggers/indexes, NOCASE alternatives, partial or
expression substitutes, WITHOUT ROWID variants, missing constraints, malformed
JSON/evidence, altered identities or inconsistent bodies/digests refuse access.
There is no schema repair, migration, deletion, automatic retention or ALTER path.

Bodies are untrusted parsed values, not signed work. Null has tag 0 and an empty
BLOB; every string, including empty, has tag 1. Strings encode/decode with UTF-8
surrogatepass, preserving combining characters, NUL/newlines, non-BMP and lone
surrogates without SQLite TEXT conversion or normalization. This preserves body
values, not the raw JSON escape spelling or other comment metadata.

SHA-256 digests use domain/version prefixes and length-framed parts. A body
digest binds its tag and bytes. A batch digest binds store/request identity,
canonical evidence and each ordinal/comment ID/tag/body/digest. Replay compares
actual bytes as well as digests. These are consistency checks, not signatures,
credential protection, tamper-proof history or source attestation. A privileged
database writer can forge a mutually consistent record.

Every attempt keeps its own historical bodies. Repeated or changed comment IDs
in another attempt do not overwrite earlier rows, deduplicate across attempts or
establish an authoritative latest version.

## Result bounds and honest coverage

Require exact accepted SessionResult, SessionEvidence and CommentObservation
types, not dictionary lookalikes/subclasses. Validate outcome/coverage/code types,
counter ranges and relationships, request/observation identity, duplicate IDs,
tag/body types and hint confinement before insertion. Successful unique count
equals tuple length; unique+duplicate counts equal entries. Calls/pages <=4,
entries <=100, and validated bytes are bounded by accepted page counts. Successful
sessions require 1..4 calls/pages; BUDGET_STOPPED requires four and the correct
next-page hint relative to the reserved initial page. Failed results require
observations=None and no next hint; provisional counts do not imply saved bodies.

The logical per-attempt storage bound is 100 observations and 4,000,000 encoded
body bytes plus an 8,192-byte canonical evidence bound. Oversize values refuse
before insertion, never truncate. The store checks aggregate consistency, not
the original page JSON/headers, which SessionResult does not retain. Logical
bounds are not a whole-file quota, hard RSS bound or authentication of caller
claims. There is no claim that a hand-constructed result came from real pages.

Coverage remains exactly PARTIAL_OR_UNKNOWN after reopen, including an empty
successful tuple. A stored next hint is unauthenticated routing metadata, not a
snapshot, authoritative cursor, thread-completeness or freshness guarantee. No
automatic hint following, global last-seen cursor, work delivery, admission or
exactly-once claim exists. For empty/failed results, request context comes from
the reservation and caller, not an independently attested SessionResult field.

Inspection/receipts expose only evidence and IDs/digests. Body values remain in
the separate read_observations interface and are excluded from observation repr.
They are plaintext BLOB data on disk and in memory, not encrypted or zeroized.
Do not serialize complete observation results into logs. Tests use synthetic
values only; persisting actual GitHub bodies is not authorized by this slice.

## Durability and recovery limits

Write connections verify WAL, set/read back FULL synchronous and foreign keys,
use a 250ms busy timeout and BEGIN IMMEDIATE, and never downgrade settings.
Initialization synchronizes the parent directory after confirmed database commit
and closure. Per-connection configuration is not inferred from earlier handles.
WAL and SHM belong with the database; never delete them or copy only the main
file as a recovery method. SQLite engine reads may participate in WAL recovery;
inspection-only means no application data/state mutation or polling action.

Fault tests cover insert/evidence failures, failed setup, disk/I/O-style errors,
directory sync failure, commit-before/after errors, cancellation at commit, and
real process exit after reservation and immediately before/after recording commit.
Reopen must reveal unresolved PENDING with no partial rows or the full consistent
record. It never converts PENDING into success, repairs a store or resumes a read.

The process-local stop cannot survive every possible first-write failure, and an
on-disk record cannot prove caller acknowledgment. This does not solve global
HALT, cross-process global-stop persistence or external exactly-once execution.
Explicit calls in another process are not authenticated operator decisions. No
automatic recovery writer or polling loop is provided. Existing commissioning
blockers and prior scoped failure windows remain open and unwaived.

Use one host/filesystem with appropriate SQLite locking/sync semantics. Offline
tests do not establish Railway storage durability, power-loss behavior, backups,
real-data custody, encryption, retention or access control. A deployment needs
those decisions and operational recovery design separately.

Primary references retained from the design:
- [SQLite WAL and WAL-reset fix](https://www.sqlite.org/wal.html)
- [SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous)
- [SQLite transactions and error behavior](https://www.sqlite.org/lang_transaction.html)

## Verification evidence

Author: SKIPPY, isolated Linux, Python 3.12.14, SQLite 3.53.1, pytest 8.4.2.
This is author evidence, not independent review, Homebase Windows or GitHub CI.

- New-store plus accepted-session tests: **187 passed** in 1.81s, exit 0,
  including **103 new storage cases**.
- Full suite: **2,126 passed**, 45 ResourceTracker cleanup warnings in existing
  adapter tests, 105.69s, exit 0. No failed/skipped cases were reported.
  Baseline: 2,023 passed. File-backed multiprocessing-safe harness blocked
  socket.socket.connect in parent and reloaded spawned processes.
- Process-local negative controls with repository source unchanged:
  - Premature per-observation commit: 3 expected failures, 1 pass, 99 deselected.
  - Missing unresolved-reservation gate: 1 expected failure, 102 deselected.
  - Null/empty collapse: 1 expected failure, 8 passes, 94 deselected.
  - Attempted automatic pending-row clear: rejected by query-only mode and caught
    by the inspection test (1 expected failure, 102 deselected).
  Each control returned pytest exit 1 as expected. These are mutation results,
  not failures of the final implementation.
- Process-contention tests use four independent spawned writers for same and
  different IDs; exactly one reservation permission is granted. Crash tests use
  actual os._exit in spawned children. All database files are disposable fixtures.
- All 158 materialized baseline blobs verified unchanged. Only this new evidence
  document was finalized after the successful final runtime/test full run.

## Parking and next checkpoint

Independent review/acceptance is pending. No merge, PR lifecycle/head/base change,
workflow dispatch, live request, new GitHub status post, Railway change, credential
access, marker/volume alteration, runner activation, admission, posting or trading
occurred in this implementation. Railway stays disarmed on its read-only closeout
command; preserve auto-deploy disabled, one replica, restart policy Never, both
attempt markers and existing volume. Colin remains on standby.
