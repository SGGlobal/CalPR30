"""Explicit one-shot read probe; never admit or execute comment contents."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path

from skippy.collaboration.github_comment_probe_wrapper import ProbeRefusal, observe_issue23_once
from skippy.collaboration.github_http_transport import production_transport

ARM_VALUE = "ISSUE23_PAGE1_ONCE"
MARKER = "issue23-page1.attempted"


def _reserve(directory: str) -> None:
    # An existing marker is never removed, including after any local/remote failure.
    root = Path(directory)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("state directory refused")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        try:
            os.write(fd, b"ATTEMPT_RESERVED\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main() -> int:
    if len(sys.argv) != 1:
        print('{"outcome":"REFUSED","reason":"arguments refused"}', flush=True)
        return 2
    if os.environ.get("QLA_PROBE_ARM") != ARM_VALUE:
        print('{"outcome":"REFUSED","reason":"probe not armed"}', flush=True)
        return 2
    token = os.environ.get("QLA_PROBE_GITHUB_TOKEN", "")
    if re.fullmatch(r"[A-Za-z0-9_]{1,512}", token, re.ASCII) is None:
        print('{"outcome":"REFUSED","reason":"credential configuration refused"}', flush=True)
        return 2
    directory = os.environ.get("QLA_PROBE_STATE_DIR", "")
    # Require Railway's declared mount to match the reservation directory.
    # This checks configuration; the operator must attach a persistent volume.
    if not directory or directory != os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        print('{"outcome":"REFUSED","reason":"persistent volume configuration required"}', flush=True)
        return 2
    try:
        _reserve(directory)
    except Exception:
        print('{"outcome":"REFUSED","reason":"reservation unavailable or already attempted"}', flush=True)
        return 2
    try:
        summary = observe_issue23_once(production_transport(), token)
    except ProbeRefusal as error:
        print(json.dumps({"outcome": "UNCERTAIN", "transport_calls": error.calls,
                          "status": error.status, "reservation_retained": True}), flush=True)
        return 1
    except (Exception, KeyboardInterrupt, SystemExit):
        print('{"outcome":"UNCERTAIN","reservation_retained":true}', flush=True)
        return 1
    print(json.dumps({"outcome": "OBSERVED", **asdict(summary),
                      "reservation_retained": True}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
