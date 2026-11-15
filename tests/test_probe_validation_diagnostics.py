"""End-to-end classification through real wrapper/reader and fake transport."""
import json
import sys

import pytest

from skippy.collaboration import github_comment_probe_wrapper as wrapper
from skippy.collaboration import railway_comment_probe as cli
from skippy.collaboration.github_issue_comment_reader import ReaderRefusal
from skippy.collaboration.github_live_provider import HTTPResponse

TOKEN = "synthetic_private_test_token"
JSON_HEADERS = {"content-type": "application/json"}


@pytest.mark.parametrize("response,code", [
    (HTTPResponse(200, b"[]", {}), "CONTENT_TYPE"),
    (HTTPResponse(200, b"[]", {"content-type": TOKEN}), "CONTENT_TYPE"),
    (HTTPResponse(200, TOKEN.encode(), JSON_HEADERS), "JSON"),
    (HTTPResponse(200, b"\xff", JSON_HEADERS), "JSON"),
    (HTTPResponse(200, b'{"x":1,"x":2}', JSON_HEADERS), "JSON"),
    (HTTPResponse(200, b"{}", JSON_HEADERS), "PAGE_STRUCTURE"),
    (HTTPResponse(200, b'[{"id":true,"body":null}]', JSON_HEADERS), "PAGE_STRUCTURE"),
    (HTTPResponse(200, b'[{"id":1,"body":null},{"id":1,"body":null}]',
                  JSON_HEADERS), "PAGE_STRUCTURE"),
    (HTTPResponse(200, b"[]", {**JSON_HEADERS, "link": TOKEN}), "PAGINATION"),
    (HTTPResponse(200, b"x" * 1_000_001, JSON_HEADERS), "BODY_BOUNDS"),
    (HTTPResponse(403, TOKEN.encode(), JSON_HEADERS), "RESPONSE"),
])
def test_launcher_preserves_evidence_and_redacts(response, code, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["probe"])
    monkeypatch.setenv("QLA_PROBE_ARM", cli.ARM_VALUE)
    monkeypatch.setenv("QLA_PROBE_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("QLA_PROBE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    calls = []
    def transport(*args):
        calls.append(args)
        return response
    monkeypatch.setattr(cli, "production_transport", lambda: transport)
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert json.loads(output) == {
        "outcome": "UNCERTAIN", "transport_calls": 1, "status": response.status,
        "validation_code": code, "reservation_retained": True,
    }
    assert (tmp_path / cli.MARKER).exists()
    assert cli.main() == 2 and len(calls) == 1


@pytest.mark.parametrize("code", [TOKEN, None, [], {}, 123])
def test_foreign_classification_cannot_escape(code, monkeypatch):
    def reader(*args, **kwargs):
        raise ReaderRefusal(TOKEN, 1, validation_code=code)
    monkeypatch.setattr(wrapper, "read_comment_page", reader)
    with pytest.raises(wrapper.ProbeRefusal) as caught:
        wrapper.observe_issue23_once(lambda *args: None, TOKEN)
    assert caught.value.validation_code == "UNCLASSIFIED"
    assert TOKEN not in str(caught.value)


def test_transport_failure_has_static_classification():
    def transport(*args):
        raise TimeoutError(TOKEN)
    with pytest.raises(wrapper.ProbeRefusal) as caught:
        wrapper.observe_issue23_once(transport, TOKEN)
    assert caught.value.validation_code == "TRANSPORT"
    assert caught.value.calls == 1 and caught.value.status is None
    assert TOKEN not in str(caught.value)
