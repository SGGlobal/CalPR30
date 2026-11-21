"""Single approved CODEX/CAL result path for the existing reply worker.

The worker supplies a verified inbound receipt, its GitHub helpers, and CAL's
trusted approval and HALT checks. Importing this module starts no work.
"""

import json
import os
import re
import stat
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest

from check_cal_bridge_status import UNIT, check_status

REPO = "SGGlobal/CalPR30"
ACTION = "CAL_BRIDGE_STATUS"
CONTROL_DIR = "/etc/cal-option2"
APPROVAL_FIELDS = {"action", "handoff", "comment_id", "body_sha256", "expires_at"}


def _root_control(info):
    return info.st_uid == 0 and not info.st_mode & 0o022


def load_local_approval():
    """Return the current one-shot server-owner approval, or None when halted."""
    try:
        directory = os.open(CONTROL_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not _root_control(os.fstat(directory)):
                return None
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open("approval.json", flags, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or not _root_control(info):
                    return None
                with open(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(2049)
                if len(raw) > 2048:
                    return None
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)

        approval = json.loads(raw)
        if (
            type(approval) is not dict
            or approval.keys() != APPROVAL_FIELDS
            or approval["action"] != ACTION
            or not re.fullmatch(r"QLA-ARP29-PR30-[A-Z0-9][A-Z0-9-]{0,48}", approval["handoff"])
            or type(approval["comment_id"]) is not int
            or not 0 < approval["comment_id"] < 2**63
            or not re.fullmatch(r"[0-9a-f]{64}", approval["body_sha256"])
        ):
            return None
        expires = datetime.fromisoformat(approval["expires_at"])
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            return None
        approval["expires_at"] = expires
        return approval
    except (OSError, ValueError, TypeError):
        return None


def local_halt_active(approval):
    """Treat missing, expired, or changed approval as a stop."""
    return approval is None or load_local_approval() != approval


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


def maybe_deliver_status(db, token, row, *, github, find_reply, record, owner_id):
    """Call immediately after the existing worker verifies the wake-up body."""
    approval = load_local_approval()
    if approval is None or row[0] != approval["handoff"]:
        return False

    def read_work_order(github_token, comment_id):
        comment = github(f"repos/{REPO}/issues/comments/{comment_id}", github_token)
        if (
            type(comment) is not dict
            or comment.get("id") != comment_id
            or comment.get("issue_url") != f"https://api.github.com/repos/{REPO}/issues/26"
            or type(comment.get("user")) is not dict
            or comment["user"].get("id") != owner_id
        ):
            raise ValueError("CODEX/CAL work-order author or route mismatch")
        return comment.get("body")

    def post_reply(github_token, number, body):
        posted = github(f"repos/{REPO}/issues/{number}/comments", github_token, body)
        if type(posted) is not dict or type(posted.get("id")) is not int:
            raise ValueError("CODEX/CAL POST did not return a comment ID")
        return posted["id"]

    return deliver_status(
        db, token, row, approval=approval, read_work_order=read_work_order,
        halt_active=lambda: local_halt_active(approval), find_reply=find_reply,
        post_reply=post_reply, record=record,
    )
