# CODEX/CAL (Bridge Option 2)

Option 2 lets Codex ask CAL to perform a narrowly defined diagnostic on Contabo and receive the result through GitHub. The existing path through PR #30 has returned a matching receipt acknowledgment. This pilot adds one action and a contract for returning a task result. The GitHub-to-CAL action path still needs commissioning on Contabo.

## First diagnostic

`CAL_BRIDGE_STATUS` checks only `cal-arp29-webhook.service` in CAL's systemd user manager. CAL runs `python3 scripts/check_cal_bridge_status.py` from its user context with the systemd user bus available. The probe accepts no work-order fields as command arguments. It returns one JSON object with the action, check time, unit, and status.

| Result | Meaning |
| --- | --- |
| `RUNNING` | The local receiver unit reports loaded, active, and running. |
| `NOT_RUNNING` | The service manager returned a recognized state outside those values. |
| `BLOCKED` | The probe could not establish a recognized state. |

A `RUNNING` result covers the local unit. A matching GitHub result posted by CAL is the acceptance evidence for the work-order round trip.

## Commission the round trip

1. Use PR #26 for the owner-approved work order and PR #30 for the wake-up. Pin the immutable GitHub repository ID, approved exact work-order comment and body digest, action, handoff ID and expiry in CAL's trusted action configuration. The GitHub webhook signature authenticates delivery, while the pinned approval authorizes the action. Enforce the existing HALT check before dispatch.
2. Validate the delivery and the approved work-order binding before reserving the handoff in durable state. Map only `CAL_BRIDGE_STATUS` to the fixed local probe. Repeated deliveries use the stored outcome. A pending or uncertain result requires reconciliation before any retry.
3. Return the probe's fields and the original handoff and inbox comment IDs to GitHub. Reconcile by those IDs before retrying a result post. Credentials, raw logs, and command output outside the probe schema stay on CAL.
4. Verify one real diagnostic result on PR #30 and confirm that a repeated wake-up produces no second result. Record the verification on PR #26.

Commissioning requires access to CAL's existing receiver and result worker. The pilot has no shell-command work order, service-control action, or trading action.
