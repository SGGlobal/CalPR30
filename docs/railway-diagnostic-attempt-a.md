# Dedicated issue #23 diagnostic attempt A

Baseline: `3ca7aa585d4a3a80ab01ca7f8743ffa9edb2f226`.
Attempt ID: `ISSUE23_PAGE1_DIAGNOSTIC_20261115_A`.

Chief authorized implementing and testing this dedicated diagnostic launcher.
This implementation does not deploy or run it, commission the bridge, activate
a runner, admit comment contents, post messages, merge PRs, or enable trading.
`execution_enabled=False` remains mandatory. Colin remains on standby.

## Scope

Adds only the diagnostic launcher, its synthetic tests, and this runbook. The
legacy launcher, strict reader, credential wrapper, HTTPS transport, Dockerfile,
dependencies, workflows, protected ledgers, and existing PR branches are unchanged.

The diagnostic delegates to `observe_issue23_once` at most once. Its fixed target
is `https://api.github.com/repos/SGGlobal/QLA/issues/23/comments?per_page=25&page=1`.
The existing wrapper requires GET, a 10-second transport timeout and strict
response validation. The existing transport bounds the body to 1,000,000 bytes,
validates TLS, rejects redirects, and performs no retry. Socket timeout is not
a guaranteed total wall-clock deadline. Pagination metadata is summarized,
never followed. An OBSERVED outcome establishes neither complete thread retrieval
nor permission to execute comments. The original failure's cause remains unknown.

## Two preserved reservations

The new launcher requires the state directory and Railway mount configuration
to both equal the fixed `/data` path. It opens the directory without following
a final-component symlink. It checks the original `issue23-page1.attempted`
using metadata only: a regular file, 17 bytes, one hard link. It does not open,
read, write, remove, rename or replace that file. Metadata does not authenticate
its contents. Its fingerprint is checked again after the new reservation syncs.

The separate fixed marker is:
`/data/issue23-page1-diagnostic-20261115-a.attempted`.
It is created exclusively with mode 0600. The full fixed attempt ID plus newline
must be written; both the file and directory must synchronize before transport
construction. A short write or sync failure stops before networking and leaves
any created marker in place. No reset or custom attempt-ID option exists.
Existing markers of any kind refuse the attempt. Concurrent processes sharing
the same directory permit one successful reservation.

Both markers must remain on the existing `qla-volume`. Changing the volume,
deleting markers, modifying code/constants or using the wrapper directly would
defeat these operational bounds. The launcher is not a general authorization
system and does not defend against a privileged process changing storage during
execution. Configuration matching cannot independently prove mounted persistence.
A crash after reservation can consume the attempt without sending a request.

## Proposed manual run configuration — not applied

Use the reviewed commit containing these three files. Keep automatic deploys
disabled, one replica, restart policy Never, no cron, and no healthcheck restart.
Before executing, verify the source SHA and the same volume/mount. Use the
existing inspection command to confirm the original marker; separately inspect
that the new diagnostic marker is absent, without creating it. If it already
exists, stop and investigate; never reset either reservation.

- `QLA_PROBE_STATE_DIR=/data`
- `RAILWAY_VOLUME_MOUNT_PATH=/data` (Railway-provided mount declaration)
- `QLA_DIAGNOSTIC_ARM=ISSUE23_PAGE1_DIAGNOSTIC_20261115_A`
- `QLA_PROBE_GITHUB_TOKEN`: operator-managed, fine-grained token restricted to
  SGGlobal/QLA with Issues read-only and required repository metadata access.
  Keep the token in Railway's private variable UI; never paste it in chat or logs.
- Proposed start command: `python -m skippy.collaboration.railway_diagnostic_probe`

The legacy `QLA_PROBE_ARM` value cannot arm this launcher. A separate explicit
decision to execute this newly prepared attempt is required; the implementation
authorization does not change Railway configuration or consume a live attempt.

## Results and stop behavior

Every JSON record includes the fixed attempt ID. OBSERVED includes only the
existing sanitized summary (status, delegated call count, byte count/hash,
comment count/ID range, pagination and canonical next token), not response bodies
or credential headers. UNCERTAIN includes actual delegated calls, status when
known, an allowlisted validation code or UNCLASSIFIED, and reservation retention.
Factory failure has zero delegated calls. Delegated calls are transport
invocations, not proof of server receipt or lower-level packet counts.

REFUSED reports zero delegated calls. Reservation errors report
`UNKNOWN_OR_EXISTING`: a marker may already exist or have been partially written.
Do not treat refusal as permission to retry. Do not display foreign exception
text. Logs are evidence summaries, not durable authenticated completion receipts.
After any result, retain both markers and stop. Do not commission the bridge or
automatically start another attempt. Review the sanitized result before any
further operational decision.

## Offline validation

Synthetic tests replace the production transport factory and use synthetic
credentials and disposable directories. They cover successful strict observation,
original bytes/inode/metadata preservation, old and new restart refusals,
configuration errors, Unicode token rejection, invalid or missing original
markers, pre-existing diagnostic markers, storage failures/short writes,
reservation sync before transport construction, changed original metadata,
symlink mount refusal, redaction, timeout/cancellation, validation codes,
numeric pagination normalization without follow, and eight-process reservation
contention. The process contention test uses Linux fork and skips where unavailable.

Author validation: isolated Linux, Python 3.12.14, pytest 8.4.2. All 152 baseline
Python/config blobs were verified against Git blob SHAs and remain unchanged.

- Initial focused run: 283 passed before the final two diagnostic tests were added.
- Final full suite including all 34 new diagnostic cases: **1,939 passed**,
  15 ResourceTracker cleanup warnings in existing adapter tests, 104.02 seconds,
  exit 0. No failed or skipped cases were reported.
- The successful full run used a file-backed pytest driver with an `__main__`
  guard so spawn-based tests could reload it. The driver replaced
  `socket.socket.connect` with a function raising AssertionError, including when
  reloaded in spawned children. An earlier stdin-driven full run was interrupted
  because the multiprocessing tests could not reload that driver; it is not
  counted as completed validation.
- Unarmed module smoke check: REFUSED, transport_calls=0, exit 2.

These are author results, not independent review or GitHub CI. No live project
request, real credential, Railway configuration change or marker reset occurred.
