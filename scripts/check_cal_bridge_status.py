"""Fixed, read-only status probe for the CODEX/CAL bridge pilot."""

import json
import subprocess
from datetime import datetime, timezone

UNIT = "cal-arp29-webhook.service"
FIELDS = {
    "LoadState": {"loaded", "not-found", "bad-setting", "error", "masked"},
    "ActiveState": {
        "active", "reloading", "inactive", "failed", "activating", "deactivating",
        "maintenance", "refreshing",
    },
    "SubState": {
        "dead", "condition", "start-pre", "start", "start-post", "running",
        "exited", "reload", "stop", "stop-watchdog", "stop-sigterm", "stop-sigkill",
        "stop-post", "final-watchdog", "final-sigterm", "final-sigkill", "failed",
        "dead-before-auto-restart", "failed-before-auto-restart", "dead-resources-pinned",
        "auto-restart", "auto-restart-queued", "cleaning", "mounting", "unmounting",
    },
}


def check_status():
    report = {
        "action": "CAL_BRIDGE_STATUS",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "BLOCKED",
        "unit": UNIT,
    }
    try:
        result = subprocess.run(
            [
                "/usr/bin/systemctl", "--user", "--no-pager", "show",
                *(f"--property={name}" for name in FIELDS),
                UNIT,
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return report
    if result.returncode:
        return report

    properties = {}
    for line in result.stdout.splitlines():
        name, separator, value = line.partition("=")
        if not separator or name in properties or value not in FIELDS.get(name, ()):
            return report
        properties[name] = value
    if properties.keys() != FIELDS.keys():
        return report

    running = (
        properties["LoadState"] == "loaded"
        and properties["ActiveState"] == "active"
        and properties["SubState"] == "running"
    )
    report.update(properties)
    report["status"] = "RUNNING" if running else "NOT_RUNNING"
    return report


def main():
    report = check_status()
    print(json.dumps(report, sort_keys=True))
    return {"RUNNING": 0, "BLOCKED": 1, "NOT_RUNNING": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
