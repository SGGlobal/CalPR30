# BRIDGE-01 — SKIPPY ↔ Jason checkpoint handoff (development kickoff)

**Status:** AUTHORIZED FOR OFFLINE IMPLEMENTATION / NOT CONNECTED.
**Authority:** Chief authorized launching this bridge-development checkpoint on 2026-11-07. This approval **does not** authorize PR creation, merging, deployment, external AI connections, or financial execution.
**Development branch:** `feature/bridge01-checkpoint-handoff-ledger`
**Fork point:** `phase-8-tradingview-integration` at `aa0bfcae93eeff7d07b02c24bfaa11a7390a12fa`.
**Related security work:** `feature/phase9-identity-verification-halt-enforcement` at `f9021af9857e6e72809a683bf5260561ba4e4b61` is separate, unmerged, and pending independent-review corrections under 69E.3B.2F.3A. Do not assume corrections are implemented or import that branch implicitly.

## Why
Chief currently copies Jason's development reports into SKIPPY and manually copies SKIPPY review instructions back. This led to duplicate/stale checkpoint reports. Build an *offline, auditable state/handoff foundation* so participants can distinguish current work from duplicate submissions. This repository already contains a synthetic `CollaborationProtocol` with `Message`, `TaskState`, `MessageType`, idempotency keys, acknowledgment and a simulated `LocalRouter`. Reuse its invariants where possible; do not build a competing approval or HALT authority.

## BRIDGE-01 deliverable (one narrowly scoped checkpoint)
Implement a **local-only, non-executing checkpoint/handoff ledger** with:
1. **Explicit work item identity:** `project_id`, `task_id`, `checkpoint_id`, intended base branch/commit, current commit SHA (if any), originating actor, timestamp, and correlation/idempotency identifiers. Treat commit SHA as a *claim* unless reconciled against GitHub; an unverified report cannot progress to independently-verified.
2. **Durable append-only events:** assignment created, implementation submitted, review recorded, correction requested, correction submitted, CI verification recorded, PR authorized, PR opened, merge authorized, merged, and HALT/UNCERTAIN. Events are evidence/records only, **not permissions or commands**.
3. **Deterministic state projection:** distinguish `IMPLEMENTED_REPORTED`, `SOURCE_VERIFIED`, `CORRECTION_REQUESTED`, `CI_PENDING`, `CI_VERIFIED`, `PR_AUTHORIZED`, `PR_OPENED`, `MERGE_AUTHORIZED`, `MERGED`, `HALTED`, `UNCERTAIN` as appropriate; clearly separate Jason's local pytest results from independent GitHub CI, and a report of completion from accepted security review. Avoid treating 'Step complete' as 'PR ready'.
4. **Deduplication/stale control:** identical `message_id`+envelope or `idempotency_key`+operation returns duplicate/no-op; conflicting reuse is rejected. An old SHA or earlier checkpoint cannot overwrite later state. Require expected ledger revision or equivalent optimistic concurrency control on mutating writes.
5. **Human authority:** never infer Chief authorization from Jason's statements or GitHub merges. Store authorizations only as *externally supplied, explicitly reviewed evidence*, and do not create executable authorization tokens or bind to live credentials. Require a distinct authorization for PR opening versus merging; communication itself must never merge.
6. **Fail-closed uncertainty:** malformed messages, missing bindings, out-of-order transitions, inconsistent CI identifiers/commit, duplicate conflicts, authority mismatch, and unexpected errors never advance state or trigger side effects. Preserve audit evidence for rejected events without promoting work.
7. **Minimal read-only status view:** show current checkpoint, last reviewed SHA, last reported local tests, independent CI verification (or pending), outstanding findings, and whether PR/merge authorization exists. Include concise *why rejected* for stale reports.
8. **Offline simulations and tests:** replay the exact case: PR #19 merged and CI verified at 641; Step 69E.3B.2F.3 implementation reported at `f9021af...` with 664 **locally reported** tests; independent SKIPPY review requests Step 3A corrections; repeated Step 3 report must not reset `CORRECTION_REQUESTED` or masquerade as new work. Model this as synthetic input test data, not a live repo bootstrap.

## Security and architecture constraints
- Do not wire this milestone into live Grok/xAI, OpenAI API, Gmail, Google Drive, GitHub webhooks, Railway, network listeners, TradingView, brokerage, production startup, credentials or autonomous polling. A future external adapter needs a separate explicit design, provider/permissions review, and Chief approval.
- Do not extend `LocalRouter.dispatch()` as an ungated work escape hatch. Keep new code isolated from any unprotected routing, ingestion, or execution path.
- Do not claim GitHub commit authorship, content signatures, or a source label proves that Chief approved a protected action.
- Do not add automatic retry/replay or automatic state advancement on an `UNKNOWN` authority.
- Existing `execution_enabled=False` is absolute. No funds, orders, live trading, protected execution, auto-HALT reset, autonomous recovery, merges, PR creation or deployments.
- Document what the ledger can **prove** (local internal consistency) and **cannot prove** (external sender authenticity, live provider delivery, external CI until explicitly checked).
- Keep this independent of the pending 69E.3B.2F.3A branch; do not merge or cherry-pick unreviewed changes.

## Suggested implementation
Use a `skippy/collaboration/` module with local SQLite append-only events and a read-only status projection. Explicitly define ordering and atomic update semantics. Keep any adapters in-memory/offline test doubles. Reuse existing `Message` and protocol constraints or document precise non-duplication rationale. Provide migration-free initialization for a new *synthetic-only* ledger database, not production migration.

## Acceptance tests
- Duplicated Step 3 report after correction request is marked duplicate/stale and leaves state unchanged.
- Changed content under same `message_id` or idempotency key rejects.
- New correction on new checkpoint with new SHA can be recorded without corrupting old history.
- Failed/stale expected revision rejects concurrent update.
- '664 passed locally' never becomes independently confirmed CI.
- PR and merge authorizations remain distinct, neither inferred from a report or GitHub state.
- Invalid/malformed messages, out-of-order transitions, UNKNOWN/HALTED, attempted routing/execution are rejected without effects.
- Restart rebuilds identical status from durable events.
- Existing 641-passing base regression suite remains passing; report full test count.

## Report / stopping rule
Jason should commit and push only to `feature/bridge01-checkpoint-handoff-ledger`, then return file list, full test output, new commit SHA, tests for stale report scenario, and residual limitations.
**Do not open a PR or merge without new explicit Chief authorization.**

## Later phases — NOT AUTHORIZED HERE
Actual agent-to-agent delivery will require consent and authenticated provider adapters (possibly via a GitHub issue/PR discussion or an API-based transport), identity verification, secrets handling, rate limits, delivery receipts, and hardened permission mapping. It is **not** available simply because this document or branch exists.
