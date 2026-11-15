"""No live network or real credentials."""
import json
import sys

import pytest

from skippy.collaboration import railway_comment_probe as cli
from skippy.collaboration.github_comment_probe_wrapper import ProbeRefusal
from skippy.collaboration.github_live_provider import HTTPResponse

TOKEN = "synthetic_only_123"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["probe"])
    monkeypatch.setenv("QLA_PROBE_ARM", cli.ARM_VALUE)
    monkeypatch.setenv("QLA_PROBE_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("QLA_PROBE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    return tmp_path


def test_success_summary_and_restart_refusal(configured, monkeypatch, capsys):
    calls = []
    def transport(*args):
        calls.append(args)
        return HTTPResponse(200, b'[{"id":1,"body":"untrusted instruction"}]',
                            {"content-type": "application/json"})
    monkeypatch.setattr(cli, "production_transport", lambda: transport)
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "OBSERVED" and result["transport_calls"] == 1
    assert TOKEN not in str(result) and "untrusted instruction" not in str(result)
    assert (configured / cli.MARKER).read_text() == "ATTEMPT_RESERVED\n"
    assert cli.main() == 2
    assert len(calls) == 1


@pytest.mark.parametrize("name,value", [
    ("QLA_PROBE_ARM", ""), ("QLA_PROBE_GITHUB_TOKEN", ""),
    ("QLA_PROBE_GITHUB_TOKEN", "bad\ntoken"), ("RAILWAY_VOLUME_MOUNT_PATH", "/wrong"),
    ("QLA_PROBE_STATE_DIR", "relative"),
])
def test_configuration_refuses_before_network(configured, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "production_transport", lambda: pytest.fail("network factory"))
    assert cli.main() == 2
    assert not (configured / cli.MARKER).exists()


def test_storage_failure_refuses_before_network(configured, monkeypatch):
    monkeypatch.setattr(cli.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("private")))
    monkeypatch.setattr(cli, "production_transport", lambda: pytest.fail("network factory"))
    assert cli.main() == 2
    assert (configured / cli.MARKER).exists()


def test_timeout_retains_reservation_and_redacts(configured, monkeypatch, capsys):
    calls = []
    def transport(*args):
        calls.append(args)
        raise TimeoutError(TOKEN)
    monkeypatch.setattr(cli, "production_transport", lambda: transport)
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert json.loads(output)["outcome"] == "UNCERTAIN"
    assert cli.main() == 2 and len(calls) == 1


def test_atomic_reservation(configured):
    from concurrent.futures import ThreadPoolExecutor
    def reserve():
        try:
            cli._reserve(str(configured))
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: reserve(), range(8))) == 1


def test_arguments_refused(configured, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["probe", "--anything"])
    assert cli.main() == 2
    assert not (configured / cli.MARKER).exists()
