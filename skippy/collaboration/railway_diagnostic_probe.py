"""Separately armed, single diagnostic observation; no work admission.

Preserves the legacy reservation. A fixed second latch is never removed, even
when reservation synchronization or the request fails. See the companion runbook.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import asdict

from skippy.collaboration.github_comment_probe_wrapper import ProbeRefusal, observe_issue23_once
from skippy.collaboration.github_http_transport import production_transport

execution_enabled = False
ATTEMPT_ID = "ISSUE23_PAGE1_DIAGNOSTIC_20261115_A"
STATE_DIR = "/data"
LEGACY_MARKER = "issue23-page1.attempted"
MARKER = "issue23-page1-diagnostic-20261115-a.attempted"
RESERVATION = (ATTEMPT_ID + "\n").encode("ascii")


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _legacy_info(directory_fd):
    info = os.stat(LEGACY_MARKER, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_size != 17 or info.st_nlink != 1:
        raise ValueError("legacy reservation refused")
    return info


def _reserve() -> None:
    """Reserve this fixed diagnostic on the same mount, without opening legacy data.

    Metadata checks do not authenticate legacy contents or prove persistence.
    The operator must preserve the mounted volume and both reservations.
    """
    directory_fd = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        before = _fingerprint(_legacy_info(directory_fd))
        fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        try:
            # A partial write consumes the attempt and prevents networking.
            if os.write(fd, RESERVATION) != len(RESERVATION):
                raise OSError("incomplete reservation")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory_fd)
        if _fingerprint(_legacy_info(directory_fd)) != before:
            raise ValueError("legacy reservation changed")
    finally:
        os.close(directory_fd)


def _emit(**fields):
    print(json.dumps({"attempt_id": ATTEMPT_ID, **fields}, sort_keys=True), flush=True)


def main() -> int:
    if len(sys.argv) != 1:
        _emit(outcome="REFUSED", reason="arguments refused", transport_calls=0)
        return 2
    if execution_enabled is not False or os.environ.get("QLA_DIAGNOSTIC_ARM") != ATTEMPT_ID:
        _emit(outcome="REFUSED", reason="diagnostic not armed", transport_calls=0)
        return 2
    if (os.environ.get("QLA_PROBE_STATE_DIR") != STATE_DIR or
            os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") != STATE_DIR):
        _emit(outcome="REFUSED", reason="persistent volume configuration required",
              transport_calls=0)
        return 2
    token = os.environ.get("QLA_PROBE_GITHUB_TOKEN", "")
    if re.fullmatch(r"[A-Za-z0-9_]{1,512}", token, re.ASCII) is None:
        _emit(outcome="REFUSED", reason="credential configuration refused", transport_calls=0)
        return 2
    try:
        _reserve()
    except (Exception, KeyboardInterrupt, SystemExit):
        _emit(outcome="REFUSED", reason="diagnostic reservation refused",
              transport_calls=0, reservation_state="UNKNOWN_OR_EXISTING")
        return 2
    calls = 0
    try:
        transport = production_transport()

        def counted_transport(*args):
            nonlocal calls
            calls += 1
            return transport(*args)

        summary = observe_issue23_once(counted_transport, token)
    except ProbeRefusal as error:
        _emit(outcome="UNCERTAIN", transport_calls=calls, status=error.status,
              validation_code=error.validation_code, reservation_retained=True)
        return 1
    except (Exception, KeyboardInterrupt, SystemExit):
        _emit(outcome="UNCERTAIN", transport_calls=calls, status=None,
              validation_code="UNCLASSIFIED", reservation_retained=True)
        return 1
    _emit(outcome="OBSERVED", **asdict(summary), reservation_retained=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
