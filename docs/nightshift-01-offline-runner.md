# NIGHTSHIFT-01 — One Bounded Development Handoff (Offline MVP)

**Launch date:** 2026-11-07 (America/Chicago)
**Authorization:** Chief authorized launching NIGHTSHIFT-01 development. This permits an **offline, simulated, non-executing MVP** on this feature branch only. It is **not** authorization to connect Grok/OpenAI APIs, start unattended remote workers, deploy to Railway, change protected authorities, open PRs, merge, or perform any financial activity.
**Base:** `phase-8-tradingview-integration` at merged BRIDGE-01 commit `dc94c981d45bb977a69e7424aef94c3149d280de`.
**Branch:** `feature/nightshift01-bounded-offline-runner`.

## Chief's success condition

Chief wants to approve one bounded development objective, leave the computer, and later read a trustworthy account of what SKIPPY and Jason completed. **NIGHTSHIFT-01 is the local proof-of-workflow**, not overnight service activation. Implement a deterministic simulated one-task handoff with a durable report. The future external AI transport and unattended scheduling are separate security/authorization milestones.

## Existing foundation — reuse instead of replacing
- `skippy/collaboration/protocol.py`: durable messages, task state, idempotency, HALT, coordinated work-admission binding.
- `skippy/collaboration/orchestrator.py`: `CollaborationOrchestrator.run(task_id, objective, force_correction=False)` and `report()`, simulated Skyppy/Jason, accept→dispatch→ack; reject replay and recovery without review.
- `skippy/collaboration/checkpoint_ledger.py`: BRIDGE-01's event/revision ledger, ASSIGNED→IMPLEMENTED_REPORTED→SOURCE_VERIFIED etc., with fail-closed evidence and separate Chief authorization.
- `skippy/collaboration/github_adapter.py`: synthetic/read-only provenance-aware GitHub provider. Never treat fabricated local CI as an actual GitHub CI run.
- `skippy/collaboration/coordinated_halt_gate.py`: genuine coordinated HALT authority; do not create a parallel permissive one.

**Important security debt:** `feature/phase9-identity-verification-halt-enforcement` is separate, unmerged, and still under review. Do not depend on that branch or weaken gate bindings to make NIGHTSHIFT pass tests.

## NIGHTSHIFT-01 deliverable — exactly one local workflow

Build a **`NightshiftRunner` or equivalent thin orchestration layer** (not a replacement for existing `CollaborationOrchestrator`) to:

1. **Receive a single predeclared, Chief-approved synthetic assignment** with explicit project/task/checkpoint IDs, objective, permitted step count, timeout/budget caps, and a unique assignment identifier. For this milestone, supplied approval is an offline fixture and **must not be interpreted as real authorization** for protected actions.
2. **Validate readiness before creating work:** existing genuine coordinated guard already bound; all underlying HALT/UNKNOWN checks successful; existing task and ledger not conflicting; permitted actor/source/operation. No duplicate instance or fake guard accepted. If any prerequisite fails, stop with a durable, explainable refusal.
3. **Use existing simulated agents:** one assignment, one Jason plan/implementation simulation, one Skyppy review, at most **one** correction cycle; never invoke real agent APIs, a shell, git push, network, or tools that can act on external accounts.
4. **Integrate with BRIDGE-01 ledger** using revision-aware events; record task and checkpoint identity; never claim a simulated plan is committed code or independent CI. Ledger `SOURCE_VERIFIED` is permitted only for verified *synthetic fixture evidence* within this test mode and status must say synthetic, not actual GitHub verified.
5. **Emit a durable read-only morning report** from recorded evidence (e.g., task, checkpoint, assignment ID, simulated actions, review decision, correction count, final state, claimed/test-verified distinction, warnings, outstanding required Chief approvals). Report must be reconstructable after restart and must not include secrets or misleading claims of GitHub code changes.
6. **Fail closed on uncertainty:** errors, timeout, exhaustion, stale revision, duplicate/conflicting message, missing guard, partial writes, unexpected state, or HALT stop affected workflow. Do not clear HALT; do not automatically recover, continue, retry, or catch up. Recovery is report/diagnostic only, unless Chief explicitly authorizes a new task.
7. **Stay bounded:** one task, at most one correction, deterministic maximum local steps, a monotonic elapsed-time cap and fixed in-process resource limits; zero autonomous repeated runs. Use injected clock/test doubles to make timeout and failure tests deterministic.
8. **Keep write/admission model explicit:** because `CollaborationProtocol` and `CheckpointLedger` use separate SQLite databases, **do not claim cross-database atomicity**. Order operations defensively and mark `UNCERTAIN` where a partial commit cannot be reconciled. Never dispatch if the live coordinated check fails at the point of admission; do not expose new unguarded direct handlers. Document TOCTOU limits.

## Acceptance tests (minimum)
- Golden path: one synthetic assignment → Jason simulated result → Skyppy simulated review → report from persisted state; no financial/external side effects.
- Bounded correction path: intentionally inadequate initial plan → one correction → review; no second correction permitted.
- Restart rehydrates the same report without re-running agent actions.
- Duplicate assignment / replay / conflicting ID / stale revision produce no new simulated actions.
- Missing, replaced, mismatched or HALTED coordinated guard, and UNKNOWN authority: zero work dispatched; fail-closed report.
- Timeout, budget cap, simulated failure, partial ledger/protocol persistence: stop as UNCERTAIN or blocked; no self-healing.
- Strict provenance: synthetic planning results never become real GitHub commit, independent CI, PR permission, or merge permission.
- The synthetic runner cannot call network, execute shell, modify GitHub, deploy, or enable financial execution.
- All existing QLA tests remain passing; add focused new tests and report the full count.

## Report required from Jason
Return implementation file list, tests and precise full pytest result, one illustrative successful synthetic morning report and one failure report, branch head SHA, limitations, and safety assertions. **Commit and push to the NIGHTSHIFT-01 branch only; do not open PR, merge or deploy.** Stop for Skippy independent review.

## Non-negotiable
`execution_enabled=False`; no live trading, orders, brokerage, money transfers, Railway, provider credentials, Grok/OpenAI connection, automatic protected actions, automatic HALT reset, or unattended hosted workers.

## Separately authorized later steps
**NIGHTSHIFT-02:** design/approve provider-specific authenticated communication adapters, bounded permission scopes, secrets storage, provider costs/rate limits, transport receipts and independent human approval mapping.
**NIGHTSHIFT-03:** limited unattended commissioning in a sandbox with monitoring, an explicit stop switch, and Chief-approved operating hours/budget. Each requires its own authorization and successful prior review.
