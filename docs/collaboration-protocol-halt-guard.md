# SKIPPY AI Collaboration Bridge — Direct Collaboration Protocol HALT Guard (Step 69E.3B.2E.1)

**Status:** Opt-in, offline, synthetic, non-executing. Not a production cutover. Not production-ready.

## Direct entry-point inventory

| Method | Classification | Guarded when opted in? |
| --- | --- | --- |
| `accept()` ordinary messages | Accepts or advances work | Yes |
| `accept()` `MessageType.HALT` | Protective HALT request | No — remains available |
| `track_outbound()` | Creates synthetic outbound tracking | Yes |
| `record_approval()` | Creates synthetic approval | Yes |
| `consume_approval()` | Consumes synthetic approval | Yes |
| `acknowledge()` | Bookkeeping on an accepted message | No |
| `mark_outbound_uncertain()` | Protective uncertainty marking | No |
| `simulate_round_trip()` | Helper using public `accept()` | Protected transitively |
| `halt()` | Administrative HALT write | No — protective |
| `get_state()`, `is_acknowledged()`, `unacknowledged()`, `is_halted` | Read-only | No |

Ungated instances are not coordinated-protected.

## Opt-in binding

`CollaborationProtocol.require_coordinated_guard(gate)` requires a `CoordinatedHaltGate` that references this same protocol instance. Binding records that enforcement is required, retains the original guard identity, and snapshots policy, protocol, database paths, coordinated authority path, and migration settings. A missing, replaced, or drifted guard fails closed. There is no supported removal or replacement method. Default construction remains unchanged.

## Fail-closed work operations

Guarded operations require `allowed is True`, coordinated `NOT_HALTED`, completed migration, no legacy HALT, and `execution_enabled is False`. Failed migration, unavailable authority, unknown state, legacy HALT, malformed results, and unexpected exceptions reject with `ProtocolError("Coordinated guard rejected")`. Exception details are not exposed. A failed check does not create messages, outbound records, or approvals, and does not consume an approval.

Checks run before local write locks to avoid holding a protocol write lock across multi-database migration. There is no cross-database atomicity. A HALT after a successful check and before the local transaction is a documented race.

## Remaining limitations

- Not system-wide enforcement.
- Not integrated into live startup.
- Guard binding is in-memory; a reopened protocol is ungated until it opts in again.
- `acknowledge()` and `mark_outbound_uncertain()` remain ungated by design.
- Administrative `halt()` remains available when coordinated authority is unavailable.
- Mixed-version processes can disagree.
- Secondary audit remains best-effort.

Future system-wide enforcement would require every work-acceptance entry point, including ungated policy and identity paths, to consult the coordinated authority, with legacy flags remaining authoritative until a separately authorized cutover.

`execution_enabled=False` remains mandatory.
