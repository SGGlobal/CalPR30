"""
SKIPPY AI Collaboration Bridge — Production Transport Tests.

No real GitHub contact. Every probe path injects a fake transport.
"""

import json

import pytest

from skippy.collaboration.github_http_transport import (
    ProductionHTTPTransport,
    RedirectRejectingHandler,
    production_transport,
)
from skippy.collaboration.github_live_probe import (
    ProbeCancelled,
    run_probe,
)
from skippy.collaboration.github_live_provider import (
    CredentialSafeError,
    HTTPResponse,
)


SHA = "79925e4aa80bef170778fc02a022f5c338739c91"


def _guarded_factory():
    def factory():
        raise AssertionError("Network access is not permitted in tests")

    return factory


class FakeResponse:
    def __init__(self, status, body, url):
        self.status = status
        self._body = body
        self.url = url

    def read(self, size=-1):
        if not self._body:
            return b""
        chunk = self._body[:size] if size > 0 else self._body
        self._body = self._body[len(chunk):]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append(request.full_url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_https_enforcement():
    transport = ProductionHTTPTransport()
    with pytest.raises(CredentialSafeError, match="HTTPS required"):
        transport("GET", "http://api.github.com/repos/SGGlobal/QLA", {}, 5)


def test_exact_host_validation():
    transport = ProductionHTTPTransport()
    with pytest.raises(CredentialSafeError, match="Unexpected API host"):
        transport("GET", "https://evil.example/repos", {}, 5)


def test_unexpected_port_rejected():
    transport = ProductionHTTPTransport()
    with pytest.raises(CredentialSafeError, match="Unexpected port"):
        transport(
            "GET",
            "https://api.github.com:8443/repos/SGGlobal/QLA",
            {},
            5,
        )


def test_userinfo_rejected():
    transport = ProductionHTTPTransport()
    with pytest.raises(CredentialSafeError, match="userinfo"):
        transport(
            "GET",
            "https://user:pass@api.github.com/repos/SGGlobal/QLA",
            {},
            5,
        )


def test_get_only():
    transport = ProductionHTTPTransport()
    with pytest.raises(CredentialSafeError, match="Unsupported HTTP method"):
        transport("POST", "https://api.github.com/repos/SGGlobal/QLA", {}, 5)


def test_redirect_rejected_before_follow():
    from urllib.error import HTTPError

    handler = RedirectRejectingHandler()
    with pytest.raises(CredentialSafeError, match="Unexpected redirect"):
        handler.redirect_request(
            None, None, 302, "Found", {}, "https://evil.example"
        )


def test_http_error_preserves_status():
    from urllib.error import HTTPError

    opener = FakeOpener(
        HTTPError(
            "https://api.github.com/repos/SGGlobal/QLA",
            401,
            "Unauthorized",
            hdrs=None,
            fp=None,
        )
    )
    transport = ProductionHTTPTransport(opener=opener)
    response = transport(
        "GET", "https://api.github.com/repos/SGGlobal/QLA", {}, 5
    )

    assert response.status == 401
    assert response.body == b""


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_http_error_categories(status):
    from urllib.error import HTTPError

    opener = FakeOpener(
        HTTPError(
            "https://api.github.com/repos/SGGlobal/QLA",
            status,
            "err",
            hdrs=None,
            fp=None,
        )
    )
    transport = ProductionHTTPTransport(opener=opener)
    response = transport(
        "GET", "https://api.github.com/repos/SGGlobal/QLA", {}, 5
    )

    assert response.status == status
    assert response.body == b""


def test_timeout_classification():
    opener = FakeOpener(TimeoutError("slow"))
    transport = ProductionHTTPTransport(opener=opener)

    with pytest.raises(CredentialSafeError, match="timed out"):
        transport("GET", "https://api.github.com/repos/SGGlobal/QLA", {}, 5)


def test_factory_does_not_connect():
    transport = production_transport()
    assert callable(transport)


def test_default_offline():
    report = run_probe(live=False)
    assert report["status"] == "offline"
    assert report["source"] == "synthetic"


def test_confirmation_required_before_token():
    called = {"token": False}

    def decline(prompt):
        return "no"

    def reader(prompt):
        called["token"] = True
        return "secret"

    with pytest.raises(ProbeCancelled):
        run_probe(
            live=True,
            confirmer=decline,
            token_reader=reader,
            transport_factory=_guarded_factory(),
        )

    assert called["token"] is False


def test_safe_cancellation():
    def decline(prompt):
        return ""

    with pytest.raises(ProbeCancelled, match="cancelled"):
        run_probe(
            live=True,
            confirmer=decline,
            transport_factory=_guarded_factory(),
        )


def test_missing_credentials_fail_closed():
    def accept(prompt):
        return "LIVE"

    def empty(prompt):
        return ""

    with pytest.raises(CredentialSafeError, match="Missing GitHub token"):
        run_probe(
            live=True,
            confirmer=accept,
            token_reader=empty,
            transport_factory=_guarded_factory(),
        )


def test_token_not_in_error():
    secret = "ghp_should_not_appear"

    def accept(prompt):
        return "LIVE"

    def reader(prompt):
        return secret

    def factory():
        def transport(method, url, headers, timeout):
            raise CredentialSafeError("GitHub request failed")

        return transport

    try:
        run_probe(
            live=True,
            confirmer=accept,
            token_reader=reader,
            transport_factory=factory,
        )
    except Exception as exc:
        assert secret not in str(exc)
    else:
        raise AssertionError("expected failure")


def test_unexpected_network_access_fails_test():
    def accept(prompt):
        return "LIVE"

    def reader(prompt):
        return "token"

    with pytest.raises(AssertionError, match="Network access is not permitted"):
        run_probe(
            live=True,
            confirmer=accept,
            token_reader=reader,
            transport_factory=_guarded_factory(),
        )


def test_mocked_probe_completes_offline():
    def accept(prompt):
        return "LIVE"

    def reader(prompt):
        return "token"

    def factory():
        def transport(method, url, headers, timeout):
            if "/branches/" in url:
                body = {
                    "name": "phase-8-tradingview-integration",
                    "commit": {"sha": SHA},
                }
            elif "/commits/" in url:
                body = {"sha": SHA, "commit": {"message": "checkpoint"}}
            elif "/actions/runs" in url:
                body = {"workflow_runs": []}
            else:
                body = {
                    "full_name": "SGGlobal/QLA",
                    "default_branch": "phase-8-tradingview-integration",
                    "private": True,
                }
            return HTTPResponse(
                status=200,
                body=json.dumps(body).encode("utf-8"),
                headers={},
            )

        return transport

    report = run_probe(
        live=True,
        confirmer=accept,
        token_reader=reader,
        transport_factory=factory,
    )

    assert report["status"] == "completed"
    assert report["source"] == "github_live"
    assert report["sha"] == SHA
    assert report["ci_status"] == "UNAVAILABLE"


def test_no_write_operations_on_transport():
    methods = {
        name
        for name in dir(ProductionHTTPTransport)
        if not name.startswith("_")
    }
    forbidden = {"post", "put", "delete", "push", "merge"}
    assert methods.isdisjoint(forbidden)


def test_no_trading_side_effects(tmp_path):
    from skippy.collaboration.protocol import CollaborationProtocol

    protocol = CollaborationProtocol(tmp_path / "bridge.db")
    assert protocol.is_halted is False
