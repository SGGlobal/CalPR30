"""Install the dormant Option 2 hook for the existing CAL reply timer."""

import os
import stat
import tempfile
from pathlib import Path

WORKER = Path("/home/cal/.local/state/cal-arp29/reply-channels/reply.py")
APPROVAL = Path("/etc/cal-option2/approval.json")
MODULES = ("check_cal_bridge_status.py", "cal_option2_reply.py")
IMPORT = "from cal_option2_reply import maybe_deliver_status\n"
ANCHOR = (
    "    if not comment_for(token, number, identifier, inbound):\n"
    '        raise ValueError("Inbound body, author or route changed")\n'
)
HOOK = (
    "    if maybe_deliver_status(\n"
    "        db, token, row, github=github, find_reply=find_reply,\n"
    "        record=record, owner_id=OWNER_ID,\n"
    "    ):\n"
    "        return\n"
)


def patched_worker(source):
    if not source.startswith("import json\n") or source.count(ANCHOR) != 1 or IMPORT in source:
        raise ValueError("Reply worker differs from the inspected source; installation stopped")
    return IMPORT + source.replace(ANCHOR, ANCHOR + HOOK, 1)


def install(bundle):
    if os.geteuid() != 0 or os.path.lexists(APPROVAL):
        raise ValueError("Installation requires root and a halted Option 2")
    worker = WORKER
    if worker.resolve() != worker or not stat.S_ISREG(worker.lstat().st_mode):
        raise ValueError("Unexpected reply worker path")
    original = worker.read_text()
    metadata = worker.stat()
    replacement = patched_worker(original)
    compile(replacement, str(worker), "exec")

    modules = {name: (bundle / name).read_bytes() for name in MODULES}
    for name, content in modules.items():
        compile(content, name, "exec")
    backup = worker.with_name("reply.py.before-option2")
    if any(os.path.lexists(path) for path in (backup, *(worker.parent / name for name in modules))):
        raise ValueError("An Option 2 install or backup already exists")

    created = []
    with tempfile.NamedTemporaryFile(dir=worker.parent, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            temporary.write(replacement.encode("utf-8"))
            temporary.flush()
            os.fchown(temporary.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(temporary.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fsync(temporary.fileno())

            for name, content in modules.items():
                destination = worker.parent / name
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                created.append(destination)
                with os.fdopen(fd, "wb") as output:
                    os.fchmod(output.fileno(), 0o644)
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())

            if worker.read_text() != original or os.path.lexists(APPROVAL):
                raise ValueError("CAL changed during installation")
            os.link(worker, backup)
            created.append(backup)
            os.replace(staged, worker)
        except Exception:
            staged.unlink(missing_ok=True)
            for path in reversed(created):
                path.unlink()
            raise
    print(f"Option 2 hook installed, halted. Worker backup: {backup}")


if __name__ == "__main__":
    try:
        install(Path(__file__).resolve().parent)
    except (OSError, ValueError, SyntaxError) as error:
        raise SystemExit(f"Option 2 install stopped: {error}") from None
