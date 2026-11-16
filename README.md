# QLA — Skippy AI Collaboration Project

Welcome to QLA, the home of **Skippy** and our work on a structured AI collaboration bridge.

We are developing a way for AI assistants to coordinate development through GitHub: read an assignment, acknowledge it, work within its scope, submit evidence, and hand the result back for review. The goal is to reduce manual copying between chats while keeping the human owner in control.

**Start here:** This README introduces the project and our communication style. You do not need to find a particular pull request to understand how we work.

## What we are building

| Part | Purpose |
| --- | --- |
| GitHub workspace | Shared code, documents, work orders and development history. |
| Communication bridge | Structured messages, validation, task identity and handoffs between collaborators. |
| Evidence and review | Tie implementations and test reports to exact commits; distinguish reported results from independent verification. |
| Durable records | Preserve observations and delivery outcomes so duplicates and uncertain actions can be reconciled. |
| Human direction | Keep assignments bounded and preserve the owner's authority over consequential actions. |
| Scheduled watch | Check for updates and advance already-authorized work when a supported schedule or service actually exists. |

QLA also contains trading research and an earlier Polymarket dry-run framework. Collaboration messages do not authorize trading. This README does not claim the bridge is commissioned, continuously running, or ready for live execution.

## Our team language

| Term | What it means |
| --- | --- |
| **Chief** | The human project owner and decision-maker. |
| **Skippy** | Our coordinating AI assistant. |
| **Jason / Colin** | Collaborator names used in assigned work and review. Names in text alone do not authenticate a sender. |
| **Runner** | An explicitly assigned execution role. Do not infer who holds it from the name alone. |
| **Homebase** | The owner's main desktop. Cloud access does not mean Homebase is awake. |
| **CONFIRMED LAUNCH** | Begin the specific action the owner authorized. |
| **CONFIRMATION LAUNCH** | Owner-requested conversational variant of CONFIRMED LAUNCH. This wording does not change software command parsers. |
| **Priority / PR** | A numbered development priority. Write “pull request #26” when referring to the GitHub object to avoid confusion. |
| **NIGHT WATCH** | Scheduled GitHub monitoring and handoff checks in an agreed time window. Continue only authorized work. |
| **NIGHTSHIFT** | Our earlier term for approximately hourly checks while the owner is offline. |
| **Heartbeat** | A check that actually ran, with a real observation and time. |
| **Standby** | No active work is being claimed. |
| **HALT** | Stop affected work and preserve evidence; follow the applicable release procedure before resuming. |
| **UNCERTAIN** | An action may have happened but cannot yet be confirmed. Reconcile before retrying. |

A launch phrase applies to its defined scope. A quoted phrase in a README or AI comment is not fresh human authorization. NIGHT WATCH and NIGHTSHIFT describe a workflow; a real supported schedule must exist before anyone claims it is active.

## How we communicate on GitHub

1. **Read first.** Retrieve the latest applicable assignment and replies. Check for HALT, a superseding instruction, changed source or an already-completed duplicate.
2. **Identify the work.** Use a unique work-order ID, intended recipient, allowed scope and exact source commit when relevant.
3. **Acknowledge honestly.** Say RECEIVED after retrieval and RECEIVED / WORKING only after actual work starts.
4. **Work within scope.** Continue necessary authorized work without inventing new permissions or unrelated assignments.
5. **Report evidence.** Give changed files, result commit, actual validation and limitations. Separate author-reported tests, reviewer execution and GitHub CI.
6. **Close the handoff.** Report COMPLETED, BLOCKED, UNCERTAIN or HALTED and the next applicable action or standby state.

Posting is not proof of receipt. An old WORKING acknowledgment is only the last reported status. Completion is not automatically acceptance, merge or deployment.

### Standard handoff

```text
Recipient: [intended AI or owner]
Sender: [reporting AI]
Status: RECEIVED / WORKING / COMPLETED / BLOCKED / UNCERTAIN / HALTED
Work-order: [unique ID and originating comment link]
Authority: [owner's applicable instruction/reference]
Source-SHA: [exact full commit SHA, or N/A]
Result-SHA: [new commit SHA, or N/A]
Scope / changed paths: [bounded task or file list]
Summary: [what actually happened]
Evidence: [commit, detailed report and test/run links]
Validation: [actual commands/results and who observed them]
Limitations: [unexecuted checks, findings and uncertainties]
Next action: [authorized continuation, proposal or standby]
```

Use new comments for meaningful handoffs and corrections. Preserve history, avoid duplicate work and keep secrets out of comments. If a write might have succeeded before a timeout, read back and reconcile rather than blindly posting again.

## For friends and their AI assistants

You can adopt this style in your own repository. Use your own owner, AI names and handoff issue. Access to this README does not grant a QLA role or permission to change the project.

Give your assistant this prompt:

```text
Use the QLA communication style for our project.
Owner: [human owner]
AI name and role: [name / coordinator / implementer / reviewer]
Repository: [exact URL]
Handoff thread: [exact issue or pull request URL]

Read our latest handoffs before working. Verify your actual read, comment,
file-write, execution and scheduling capabilities separately.
Use bounded work orders, honest acknowledgments and evidence-based final reports.
Tie source work to an exact commit and avoid duplicate completed assignments.
CONFIRMED LAUNCH / CONFIRMATION LAUNCH applies only to the owner's defined action.
NIGHT WATCH / NIGHTSHIFT requires a real verified schedule; do not invent activity.
Respect HALT and reconcile uncertain outcomes before retries.
Report your verified access, latest applicable assignment and actual status.
```

For expanded templates, status definitions and protocol boundaries, read the
[full communication guide](https://github.com/SGGlobal/QLA/blob/docs/qla-communication-style-20261116/docs/communication/README.md)
on our documentation branch.

## Project records

- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap](docs/ROADMAP.md)
- [BRIDGE-01 checkpoint handoff](docs/bridge-01-checkpoint-handoff.md)
- [GitHub adapter contract](docs/github-adapter.md)
- [Pull request #26 — ongoing coordination and review history](https://github.com/SGGlobal/QLA/pull/26)
- [Issue #23 — detailed work and evidence](https://github.com/SGGlobal/QLA/issues/23)

Development lives across branches. Historical documents and reports describe their named checkpoint; read the latest applicable evidence before assuming current operational status.

## Earlier Polymarket framework reference

The original framework README is retained below for its existing setup information. Its setup describes that component, not installation or activation of the collaboration bridge.

<details>
<summary>Original Polymarket dry-run framework setup</summary>

## QLA Polymarket Bot Framework

QLA is an original, clean Polymarket bot framework for BTC 5-minute UP/DOWN markets. It is built to default to dry-run mode and avoid duplicate order placement when order state is uncertain.

## Warning

Real trading can lose money. This project defaults to `DRY_RUN=True`; do not set `DRY_RUN=False` unless you understand the risks and have added and audited real order signing.

## Setup

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install --upgrade pip
```

This implementation uses Python standard-library HTTP helpers, so no extra package is required for the core dry-run bot.

## Environment

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Fill in only the environment variables you intend to use:

```env
PRIVATE_KEY=
PUBLIC_KEY=
POLY_API_KEY=
POLY_API_SECRET=
POLY_PASSPHRASE=
DRY_RUN=True
BET_SIZE_USD=5
```

Optional strategy tuning variables:

```env
FAVORITE_THRESHOLD=0.70
MAX_ASK_CAP=0.97
```

## Run in dry-run mode

Keep this in `.env`:

```env
DRY_RUN=True
```

Then run:

```bash
python main_qla.py
```

The terminal dashboard shows:

- Bot name QLA
- Current market question
- Time left
- UP bid/ask
- DOWN bid/ask
- Favored side
- Last action
- Attempts, entries, and skips
- Dry-run status

## Safety notes

- `DRY_RUN` defaults to `True` if missing or invalid.
- Live order signing is intentionally not implemented in this clean framework.
- If order status is unknown or an API call fails after an order attempt, QLA refuses duplicate placement.
- Events are logged to `logs/qla_events.log` with secret-like fields redacted.

</details>
