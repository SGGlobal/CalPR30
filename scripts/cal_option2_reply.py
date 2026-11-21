"""Single approved CODEX/CAL result path for the existing reply worker.

The worker supplies a verified inbound receipt, its GitHub helpers, and CAL's
trusted approval and HALT checks. Importing this module starts no work.
"""

from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest

from check_cal_bridge_status import UNIT, check_status

REPO = "SGGlobal/CalPR30"
ACTION = "CAL_BRIDGE_STATUS"


def deliver_status(
    db, token, row, *, approval, read_work_order, halt_active,
    find_reply, post_reply, record,
):
    """Return False for an ordinary receipt and True for the pinned pilot."""
    handoff, inbox, source, _ = row
    if handoff != approval["handoff"]:
        return False

    expected_source = (
        f"https://github.com/{REPO}/pull/26#issuecomment-{approval['comment_id']}"
    )
    if (
        source != expected_source
        or not handoff.startswith("QLA-ARP29-PR30-")
        or approval["action"] != ACTION
    ):
        raise ValueError("CODEX/CAL approval binding mismatch")

    now = datetime.now(timezone.utc)
    if now >= approval["expires_at"] or halt_active() is not False:
        raise ValueError("CODEX/CAL authorization expired or stopped")

    # The worker reads the exact PR #26 comment and checks its ID, route,
    # and immutable author ID before returning its body.
    order = read_work_order(token, approval["comment_id"])
    if type(order) is not str:
        raise ValueError("CODEX/CAL work-order body unavailable")
    digest = sha256(order.encode("utf-8")).hexdigest()
    if not compare_digest(digest, approval["body_sha256"]):
        raise ValueError("CODEX/CAL work-order digest mismatch")

    with db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS option2_results ("
            "inbox INTEGER PRIMARY KEY, handoff TEXT NOT NULL UNIQUE, body TEXT)"
        )
        won = db.execute(
            "INSERT OR IGNORE INTO option2_results "
            "SELECT ?, ?, NULL WHERE NOT EXISTS (SELECT 1 FROM replies WHERE inbox=?)",
            (inbox, handoff, inbox),
        ).rowcount
        if won:
            db.execute(
                "INSERT INTO replies (inbox, state, reply) VALUES (?, 'attempted', NULL)",
                (inbox,),
            )

    marker = f"Reply-To-Comment: {inbox}"
    if not won:
        stored = db.execute(
            "SELECT body FROM option2_results WHERE inbox=? AND handoff=?", (inbox, handoff)
        ).fetchone()
        if stored is None:
            raise ValueError("CODEX/CAL handoff or inbox already reserved")
        reply = find_reply(token, 30, marker, stored[0]) if stored[0] else None
        record(db, inbox, "verified" if reply else "held", reply)
        return True

    if datetime.now(timezone.utc) >= approval["expires_at"] or halt_active() is not False:
        raise ValueError("CODEX/CAL authorization expired or stopped before the probe")

    report = check_status()
    status = report["status"]
    if status not in {"RUNNING", "NOT_RUNNING", "BLOCKED"}:
        raise ValueError("CODEX/CAL probe status invalid")
    checked_at = datetime.fromisoformat(report["checked_at"])
    if checked_at.tzinfo is None:
        raise ValueError("CODEX/CAL probe timestamp invalid")

    body = (
        f"Recipient: SKIPPY\nSender: CAL\nAction: {ACTION}\n"
        f"Handoff-ID: {handoff}\n{marker}\n"
        f"Status: {status}\nChecked-At: {checked_at.astimezone(timezone.utc).isoformat()}\n"
        f"Unit: {UNIT}"
    )
    with db:
        db.execute("UPDATE option2_results SET body=? WHERE inbox=?", (body, inbox))

    reply = find_reply(token, 30, marker, body)
    if reply is None:
        if datetime.now(timezone.utc) >= approval["expires_at"] or halt_active() is not False:
            raise ValueError("CODEX/CAL authorization expired or stopped before posting")
        posted = post_reply(token, 30, body)
        reply = find_reply(token, 30, marker, body)
        if reply is None or reply != posted:
            raise ValueError("CODEX/CAL POST requires independent read-back")
    record(db, inbox, "verified", reply)
    return True
