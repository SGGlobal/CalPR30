"""Synthetic diagnostics only; all production transport construction is replaced."""
import json
import multiprocessing
import os
import sys

import pytest

from skippy.collaboration import railway_diagnostic_probe as cli
from skippy.collaboration import railway_comment_probe as legacy
from skippy.collaboration.github_live_provider import HTTPResponse

TOKEN = "synthetic_diagnostic_only_123"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["diagnostic"])
    monkeypatch.setattr(cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QLA_DIAGNOSTIC_ARM", cli.ATTEMPT_ID)
    monkeypatch.setenv("QLA_PROBE_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("QLA_PROBE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    (tmp_path / cli.LEGACY_MARKER).write_bytes(b"ATTEMPT_RESERVED\n")
    monkeypatch.setattr(cli, "production_transport", lambda: pytest.fail("unexpected factory"))
    return tmp_path


def identity(path):
    return path.read_bytes(), cli._fingerprint(path.stat())


def result(capsys):
    output = capsys.readouterr().out
    assert TOKEN not in output and "private response" not in output
    return json.loads(output)


def install(monkeypatch, response=None, error=None):
    calls = []
    def transport(*args):
        calls.append(args)
        if error is not None:
            raise error
        return response or HTTPResponse(200, b'[{"id":1,"body":"private response"}]',
                                        {"content-type": "application/json"})
    monkeypatch.setattr(cli, "production_transport", lambda: transport)
    return calls


def test_success_preserves_original_and_restart_cannot_send(configured, monkeypatch, capsys):
    original = configured / cli.LEGACY_MARKER
    before = identity(original)
    calls = install(monkeypatch)
    assert cli.main() == 0
    report = result(capsys)
    assert report["attempt_id"] == cli.ATTEMPT_ID
    assert report["outcome"] == "OBSERVED"
    assert report["transport_calls"] == 1 and report["status"] == 200
    assert report["reservation_retained"] is True
    assert len(calls) == 1 and calls[0][0] == "GET" and calls[0][3] == 10
    assert calls[0][1].endswith("/issues/23/comments?per_page=25&page=1")
    diagnostic = configured / cli.MARKER
    assert diagnostic.read_bytes() == cli.RESERVATION
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    assert identity(original) == before
    assert cli.main() == 2
    assert result(capsys)["transport_calls"] == 0
    assert len(calls) == 1 and identity(original) == before
    # The legacy launcher remains blocked by its original reservation too.
    with pytest.raises(FileExistsError):
        legacy._reserve(str(configured))


@pytest.mark.parametrize("name,value", [
    ("QLA_DIAGNOSTIC_ARM", ""), ("QLA_DIAGNOSTIC_ARM", legacy.ARM_VALUE),
    ("QLA_PROBE_STATE_DIR", "/wrong"), ("QLA_PROBE_STATE_DIR", "relative"),
    ("RAILWAY_VOLUME_MOUNT_PATH", "/wrong"),
    ("QLA_PROBE_GITHUB_TOKEN", ""), ("QLA_PROBE_GITHUB_TOKEN", "bad\ntoken"),
    ("QLA_PROBE_GITHUB_TOKEN", "１２３"), ("QLA_PROBE_GITHUB_TOKEN", "a" * 513),
])
def test_configuration_consumes_no_attempt(configured, monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)
    assert cli.main() == 2
    assert result(capsys)["transport_calls"] == 0
    assert not (configured / cli.MARKER).exists()


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "short", "hardlink"])
def test_legacy_metadata_required(configured, kind, capsys):
    original = configured / cli.LEGACY_MARKER
    original.unlink()
    if kind == "directory":
        original.mkdir()
    elif kind == "symlink":
        target = configured / "target"
        target.write_bytes(b"ATTEMPT_RESERVED\n")
        original.symlink_to(target)
    elif kind == "short":
        original.write_bytes(b"short")
    elif kind == "hardlink":
        original.write_bytes(b"ATTEMPT_RESERVED\n")
        os.link(original, configured / "alias")
    assert cli.main() == 2
    assert result(capsys)["transport_calls"] == 0
    assert not (configured / cli.MARKER).exists()


@pytest.mark.parametrize("kind", ["regular", "directory", "symlink"])
def test_existing_diagnostic_never_reused(configured, kind, capsys):
    marker = configured / cli.MARKER
    if kind == "regular":
        marker.write_bytes(b"partial")
    elif kind == "directory":
        marker.mkdir()
    else:
        marker.symlink_to(configured / "absent")
    assert cli.main() == 2
    assert result(capsys)["transport_calls"] == 0


@pytest.mark.parametrize("error", [TimeoutError(TOKEN), RuntimeError(TOKEN),
                                  KeyboardInterrupt(TOKEN), SystemExit(TOKEN)])
def test_remote_failure_consumes_attempt_and_redacts(configured, monkeypatch, capsys, error):
    before = identity(configured / cli.LEGACY_MARKER)
    calls = install(monkeypatch, error=error)
    assert cli.main() == 1
    report = result(capsys)
    assert report["outcome"] == "UNCERTAIN" and report["transport_calls"] == 1
    assert report["reservation_retained"] is True
    assert cli.main() == 2
    result(capsys)
    assert len(calls) == 1 and identity(configured / cli.LEGACY_MARKER) == before


def test_validation_code_and_no_pagination_follow(configured, monkeypatch, capsys):
    calls = install(monkeypatch, HTTPResponse(200, b'[]', {
        "content-type": "application/json", "link": '<https://evil.invalid>; rel="next"'}))
    assert cli.main() == 1
    report = result(capsys)
    assert report["validation_code"] == "PAGINATION" and report["status"] == 200
    assert len(calls) == 1


def test_canonical_next_is_summarized_not_followed(configured, monkeypatch, capsys):
    link = ('<https://api.github.com/repositories/1448604243/issues/23/comments'
            '?page=2&per_page=25>; rel="next"')
    calls = install(monkeypatch, HTTPResponse(200, b'[]', {
        "content-type": "application/json", "link": link}))
    assert cli.main() == 0
    assert result(capsys)["next_token"].endswith("?per_page=25&page=2")
    assert len(calls) == 1


@pytest.mark.parametrize("stage", ["write", "short_write", "file_fsync", "directory_fsync"])
def test_storage_failure_retains_consumed_marker(configured, monkeypatch, capsys, stage):
    before = identity(configured / cli.LEGACY_MARKER)
    if stage in {"write", "short_write"}:
        def write(fd, data):
            if stage == "short_write":
                return 0
            raise OSError(TOKEN)
        monkeypatch.setattr(cli.os, "write", write)
    else:
        original_fsync = cli.os.fsync
        count = 0
        def fsync(fd):
            nonlocal count
            count += 1
            if count == (1 if stage == "file_fsync" else 2):
                raise OSError(TOKEN)
            original_fsync(fd)
        monkeypatch.setattr(cli.os, "fsync", fsync)
    assert cli.main() == 2
    assert result(capsys)["transport_calls"] == 0
    assert (configured / cli.MARKER).exists()
    assert identity(configured / cli.LEGACY_MARKER) == before
    assert cli.main() == 2
    result(capsys)


def test_factory_failure_is_uncertain_zero_calls(configured, monkeypatch, capsys):
    def factory():
        raise RuntimeError(TOKEN)
    monkeypatch.setattr(cli, "production_transport", factory)
    assert cli.main() == 1
    report = result(capsys)
    assert report["validation_code"] == "UNCLASSIFIED" and report["transport_calls"] == 0
    assert (configured / cli.MARKER).exists()


def test_arguments_and_execution_flag_refuse(configured, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--reset"])
    assert cli.main() == 2
    result(capsys)
    monkeypatch.setattr(sys, "argv", ["diagnostic"])
    monkeypatch.setattr(cli, "execution_enabled", True)
    assert cli.main() == 2
    result(capsys)
    assert not (configured / cli.MARKER).exists()


def test_changed_legacy_refuses_after_reservation(configured, monkeypatch, capsys):
    original_stat = cli._legacy_info
    count = 0
    def info(fd):
        nonlocal count
        count += 1
        if count == 2:
            original = configured / cli.LEGACY_MARKER
            info = original.stat()
            os.utime(original, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        return original_stat(fd)
    monkeypatch.setattr(cli, "_legacy_info", info)
    assert cli.main() == 2
    assert result(capsys)["transport_calls"] == 0
    assert (configured / cli.MARKER).exists()


def test_reservation_synced_before_transport(configured, monkeypatch, capsys):
    synced = []
    original_fsync = cli.os.fsync
    def fsync(fd):
        original_fsync(fd)
        synced.append(os.fstat(fd).st_mode)
    monkeypatch.setattr(cli.os, "fsync", fsync)
    calls = install(monkeypatch)
    factory = cli.production_transport
    def checked_factory():
        import stat
        assert len(synced) == 2
        assert stat.S_ISREG(synced[0]) and stat.S_ISDIR(synced[1])
        assert (configured / cli.MARKER).read_bytes() == cli.RESERVATION
        return factory()
    monkeypatch.setattr(cli, "production_transport", checked_factory)
    assert cli.main() == 0
    result(capsys)
    assert len(calls) == 1


def test_symlink_mount_refused(configured, monkeypatch, capsys):
    alias = configured / "mount-alias"
    alias.symlink_to(configured, target_is_directory=True)
    monkeypatch.setattr(cli, "STATE_DIR", str(alias))
    monkeypatch.setenv("QLA_PROBE_STATE_DIR", str(alias))
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(alias))
    assert cli.main() == 2
    assert result(capsys)["transport_calls"] == 0
    assert not (configured / cli.MARKER).exists()


def _reserve_worker(queue):
    try:
        cli._reserve()
        queue.put("reserved")
    except FileExistsError:
        queue.put("existing")
    except Exception as error:
        queue.put(type(error).__name__)


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(),
                    reason="Railway/Linux process reservation test")
def test_concurrent_processes_reserve_once(configured):
    before = identity(configured / cli.LEGACY_MARKER)
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [context.Process(target=_reserve_worker, args=(queue,)) for _ in range(8)]
    try:
        for process in processes:
            process.start()
        outcomes = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert outcomes.count("reserved") == 1
        assert outcomes.count("existing") == 7
        assert identity(configured / cli.LEGACY_MARKER) == before
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()
        queue.join_thread()
