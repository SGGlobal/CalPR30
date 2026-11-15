# Railway one-shot issue #23 observation

Baseline: fbcac903a9f066c85876be9ff05899594d22bac2.
This branch adds a runnable entrypoint around the existing strict credential
wrapper and read-only HTTPS transport. Existing wrapper flags remain unchanged.
Only GET issue #23, page 1, 25 comments is permitted by the wrapper.
No comment is admitted as work. No posting, queue worker, or trading is enabled.

## Deployment

1. Disable automatic deployments before switching the source branch.
2. Select feature/railway-one-shot-comment-probe, repository root.
3. Use the included Dockerfile (Python 3.12; standard library only for this probe).
4. Attach a persistent Railway volume at /data.
5. Set QLA_PROBE_STATE_DIR=/data. Railway's RAILWAY_VOLUME_MOUNT_PATH must match.
6. Set QLA_PROBE_GITHUB_TOKEN privately in Railway variables using a fine-grained
   token limited to SGGlobal/QLA, Issues: read-only. Do not paste it into chat/logs.
7. Set QLA_PROBE_ARM=ISSUE23_PAGE1_ONCE.
8. Custom start command: python -m skippy.collaboration.railway_comment_probe
9. Set Restart Policy to Never. Leave cron and healthcheck path unset; no public domain is needed.
10. Deploy manually and inspect deployment logs.

OBSERVED means a strict response was summarized, not bridge commissioning or
complete thread retrieval. Pagination UNKNOWN makes no completeness claim.
UNCERTAIN means no automatic retry. REFUSED means launch prerequisites failed
or the attempt marker already exists. A completed one-shot process exits normally.

## Persistent attempt reservation

Before constructing the network transport, the launcher exclusively creates
/data/issue23-page1.attempted, synchronizes the file and directory, and retains it
permanently after success, timeout, cancellation, or failure. Concurrent starts
sharing the same volume permit at most one reservation. An error after creation
also retains the marker; manual investigation is required.

Guarantees depend on the same persistent volume being mounted and filesystem
durability. Configuration matching cannot prove that storage is persistent.
Replacing/deleting the volume or marker defeats the latch. Do not remove or
rename it to retry. This is a scoped probe latch, not the protected bridge ledger
or a general authorization system. A crash after reservation can consume the
attempt without sending a request. Results are sanitized summaries in logs;
they are not durable authenticated task receipts.

## Validation

Synthetic tests cover configuration refusal, success with summary-only output,
timeouts, storage synchronization failure, repeated starts and concurrent
reservation. Existing wrapper, strict reader and transport regressions are run
with fake transports. No real credential or live request is used by tests.

Local Python 3.12 validation: 202 tests passed across the launcher, wrapper,
strict reader and HTTPS transport suites. Unarmed module invocation exited 2
with a static refusal. Live networking and a Railway deployment were not tested.
