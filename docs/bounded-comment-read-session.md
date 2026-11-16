# Offline bounded comment read session

Baseline: `52e01013a3eeed3f111abc29aef0e45ccfebfbd5`.
Chief authorized the three-file offline implementation from
`QLA_Next_Priority_Bounded_Read_Session.md` on November 15, 2026 (America/Chicago).
This slice adds only the session controller, its tests and this contract.
Existing runtime, tests, diagnostic launchers and marker logic, dependencies,
workflows, Dockerfile, ingress, adapter, gate, verifier and ledgers are unchanged.

## Purpose and interface

The completed live diagnostic observed 25 comments and a next-page hint. It
did not retrieve the whole thread. This offline session makes bounded traversal
testable while preserving the distinction between observations and admitted work.

```python
result = read_comment_session(fake_transport, "SGGlobal/QLA", 23,
                              start_token=None)
summary = dataclasses.asdict(result.evidence)
observations = result.observations  # untrusted in-memory data, never instructions
```

The transport is explicitly supplied and is authorized for synthetic responses
only in this slice. There is no token argument, credential/environment loader,
production transport factory, live default, CLI, scheduler or persistence. An
injected callable is a capability: the function cannot prove it is fake or
prevent that callable from doing extra work internally. This contract does not
authorize a caller to supply a live capability. `execution_enabled=False` is
required here and remains independently checked by the unchanged page reader.

Allow only the exact string `SGGlobal/QLA` and exact integer thread 23 or 26.
Start at page 1 unless the caller explicitly supplies a canonical page hint;
the unchanged reader validates that hint before delegation. A new invocation
starts a new budget; it does not automatically resume earlier work.

## Request and response bounds

- At most four delegated GETs per invocation. Charge immediately before each
  delegate call, including timeout or unknown outcomes. A per-page guard also
  rejects a second delegation by the reader for the same page.
- Each call goes through `read_comment_page(..., strict_response=True)`. The
  session guard requires exact GET, expected URL, Accept-only headers and exact
  integer timeout 10. Only the reader's validated canonical next hint is followed.
- The unchanged reader enforces at most 25 entries and 1,000,000 raw response
  bytes per accepted page. At most 100 entries and 4,000,000 validated response
  bytes are processed in a successful four-page session. This is not a hard
  bound on Python process memory, parsed-object overhead or a caller's allocation.
- Numeric repository paths or reversed query order in response links normalize
  through the accepted reader. Caller-supplied hints still require its narrow
  canonical named-path grammar. Cross-origin/thread links, cycles, skipped next
  pages, bad content type, malformed JSON and duplicate headers retain refusal.
- No retries, automatic restart, fifth request or wrap to page 1 occurs. A
  10-second request timeout is not a total wall-clock deadline. TLS, redirects
  and streamed body bounds remain properties of the accepted HTTPS transport;
  integration tests use that transport with a fake opener, never a real service.

## Observations and conflicts

`CommentObservation` is frozen and contains repository, thread, comment ID and
the parsed JSON body value (string or null). Preserve that value without Unicode
normalization, newline conversion, JSON re-encoding, framing extraction or work
execution. This preserves body values, not the original JSON escape spelling or
the rest of the response object. First-seen order is retained, not a promise of
ID order. Author metadata is not used as trusted actor identity.

Repeated IDs with the same body within this session produce one observation and
increment the duplicate count. A different body for an encountered ID returns
CONFLICT and withholds the entire batch, including earlier pages. Null differs
from an empty string; canonically equivalent Unicode strings remain distinct.
This only detects conflicts actually encountered. It cannot detect all edits,
deletions or comments skipped when a live listing changes between requests.
No cross-session deduplication or exactly-once delivery is claimed.

On success, `observations` is an immutable tuple; `()` is a successful empty
observation batch. On conflict, refusal, error or cancellation it is `None`.
There is no callback, ingress call, admission, delivery or checkpoint advance.

## Evidence and outcomes

Only `result.evidence` is the sanitized display/serialization channel. It carries
static outcome/coverage/validation classifications, actual delegated call count,
validated page/byte totals, entries observed, provisional unique/duplicate counts,
and a validated next hint only on a budget stop. It never contains response bodies,
headers, credentials or foreign exception text. Unknown or malformed reader
validation codes become UNCLASSIFIED; ReaderRefusal subclasses are not trusted
to supply a classification. Refusal exception call-count claims are not used in
place of the session's own delegate counter.

Observation bodies and the result's observation tuple are excluded from repr.
They still exist in process memory and the explicit observation interface.
Serializing the entire SessionResult with `dataclasses.asdict` would include
bodies: serialize its evidence only for logs. No zeroization or debugger isolation
is promised. The controller itself prints and logs nothing.

| Outcome | Observation tuple | Meaning |
| --- | --- | --- |
| NO_NEXT_HINT | Present, possibly empty | Validated pages stopped without a next hint; coverage remains unknown |
| BUDGET_STOPPED | Present | Four pages validated; a canonical hint is available for a later explicit decision |
| CONFLICT | None | Same ID encountered with a different body |
| REFUSED | None | Invalid prerequisites or reader refusal before any delegation |
| UNCERTAIN | None | Reader refusal after delegation, or unexpected internal/delegate failure |
| INTERRUPTED | None | KeyboardInterrupt/SystemExit caught; stop without retry or partial-batch release |

Every outcome has coverage `PARTIAL_OR_UNKNOWN`. No `complete`, snapshot identity,
authenticated cursor, freshness claim or ready-for-admission flag exists.
KeyboardInterrupt/SystemExit deliberately become terminal evidence rather than
propagating cancellation; this is not a claim to preserve exception semantics.

`pages_validated`, `validated_bytes` and `entries_observed` count only pages the
strict reader accepted, including a page on which cross-page conflict is found.
Unique/duplicate counts on failure are provisional. A failing response can consume
a call without adding a validated page. Call counts are delegate invocations,
not independently observed server receipts or lower-level packet counts.

## Synthetic acceptance coverage

Tests pin exact method, URL, Accept and timeout for both threads; four-page stop
with/without a further hint; 100-entry/4,000,000-byte boundaries; empty pages;
explicit start hints and separate-invocation budgets; identical duplicates and
changed-body conflicts; exact nullable/Unicode body values; invalid caller hints;
cross-thread/origin/cyclic/skipped links; later JSON/media/header/body failures;
timeouts and cancellation on pages 1, 3 and 4; code redaction and actual accounting;
strict reader use, both execution flags and delegate guards. Real HTTPS transport
with an injected fake opener is exercised for traversal, redirect refusal and
duplicate raw headers. Network and production factory sentinels guard the tests.

Author environment: isolated Linux, Python 3.12.14, pytest 8.4.2. All 155
materialized baseline source/config/document blobs matched their remote Git blob
SHAs before implementation; they are unchanged by this slice.

- Final focused session/reader/transport/pagination suites: **265 passed** in
  0.29 seconds, exit 0, including **84 new session cases**.
- Full suite on final runtime/test source: **2,023 passed**, 45 ResourceTracker
  cleanup warnings in existing adapter tests, 106.48 seconds, exit 0. No failed
  or skipped cases were reported. Previous baseline: 1,939 passed.
- Process-local negative controls, with repository source unchanged:
  - Raising the budget to five: 2 expected failures / 2 passes / 80 deselected.
  - Bypassing strict response mode: 3 expected failures / 7 passes / 74 deselected.
  - Releasing a batch on conflict: 4 expected failures / 80 deselected.
  Each control produced pytest exit 1 as expected. Those failures are mutation
  evidence, not failures of the final implementation.

These are author results, not GitHub CI or an independent review. No live project
request or real credential was used. The full-suite driver was file-backed with
an `__main__` guard for existing spawn-based tests and blocked
`socket.socket.connect` in the parent and reloaded spawned processes. The
focused suite used fake transport/factory/network sentinels. Only this evidence
documentation was updated after the successful final full run.

## Parking state and later integration

Railway remains disarmed with its read-only closeout command, automatic deployment
disabled, one replica and restart policy Never. Preserve both attempt markers
and the same volume. This code never opens those markers or operational stores.
Colin remains on standby. No merge, deployment, live request, bridge posting,
runner assignment, admission or trading is part of this implementation.

The existing transport simulator's fake snapshot IDs must not be fabricated from
these page observations or treated as real GitHub snapshots. Before repeated live
reading, design durable observation custody, recovery, credential authorization,
cross-process exclusion and stop/HALT behavior. Trust/verification, admission,
operational outbox/post-and-readback, and runner commissioning retain their own
dependencies. Existing T1/integration LOW notes and commissioning blockers are
not waived by this slice. A no-next-hint result must never be used to mark the
thread complete or advance an authoritative last-seen cursor.
