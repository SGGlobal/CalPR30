"""
SKIPPY AI Collaboration Bridge — Production Transport Tests.

No real GitHub contact. Redirect and status tests use a BaseHandler
mock inside a real OpenerDirector.
"""

import json
from email.message import Message
from io import BytesIO

import pytest

from skippy.collaboration.github_http_transport import (
    ProductionHTTPTransport,
    RedirectRejectingHandler,
    build_secure_opener,
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


class MockResponse(BytesIO):
    """
    urllib-compatible response.

    Provides code, msg, info(), read(), and url so HTTPErrorProcessor
    can inspect it without network access.
    """

    def __init__(self, status, body, url, location=None):
        super().__init__(body)
        self.status = status
        self.code = status
        self.msg = "mock"
        self.url = url
        self._headers = Message()
        if location:
            self._headers["Location"] = location

    def info(self):
        return self._headers

    def geturl(self):
        return self.url


class MockHTTPSHandler:
    """
    BaseHandler-compatible mock.

    Registered with a real OpenerDirector. Returns responses rather than
    raising HTTPError so the processor chain runs.
    """

    def __init__(self, status, body=b"ok", location=None):
        from urllib.request import BaseHandler

        # Dynamically become a BaseHandler subclass instance.
        self.__class__ = type(
            "MockHTTPSHandler",
            (BaseHandler,),
            {
                "https_open": MockHTTPSHandler.https_open,
                "https_request": MockHTTPSHandler.https_request,
            },
        )
        BaseHandler.__init__(self)
        self.status = status
        self.body = body
        self.location = location
        self.requests = []

    def https_request(self, req):
        return req

    def https_open(self, req):
        self.requests.append(
            {
                "url": req.full_url,
                "headers": dict(req.headers),
            }
        )
        return MockResponse(
            self.status,
            self.body,
            req.full_url,
            self.location,
        )


def _opener_with(handler):
    from urllib.request import (
        HTTPDefaultErrorHandler,
        HTTPErrorProcessor,
        OpenerDirector,
    )

    opener = OpenerDirector()
    opener.add_handler(handler)
    opener.add_handler(RedirectRejectingHandler())
    opener.add_handler(HTTPDefaultErrorHandler())
    opener.add_handler(HTTPErrorProcessor())
    return opener


def test_http_200_succeeds():
    handler = MockHTTPSHandler(200, body=b"{\"ok\":true}")
    opener = _opener_with(handler)
    transport = ProductionHTTPTransport(opener=opener)

    response = transport(
        "GET", "https://api.github.com/repos/SGGlobal/QLA", {}, 5
    )

    assert response.status == 200
    assert len(handler.requests) == 1


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_codes_rejected_by_real_opener(code):
    handler = MockHTTPSHandler(
        code, location="https://evil.example/steal"
    )
    opener = _opener_with(handler)
    transport = ProductionHTTPTransport(opener=opener)

    with pytest.raises(CredentialSafeError, match="Unexpected redirect"):
        transport(
            "GET",
            "https://api.github.com/repos/SGGlobal/QLA",
            {"Authorization": "Bearer secret-token"},
            5,
        )

    assert len(handler.requests) == 1


def test_redirect_to_other_host_not_followed():
    handler = MockHTTPSHandler(302, location="https://evil.example/steal")
    opener = _opener_with(handler)
    transport = ProductionHTTPTransport(opener=opener)

    with pytest.raises(CredentialSafeError):
        transport(
            "GET",
            "https://api.github.com/repos/SGGlobal/QLA",
            {"Authorization": "Bearer secret-token"},
            5,
        )

    assert all("evil.example" not in item["url"] for item in handler.requests)


def test_redirect_to_approved_host_also_rejected():
    handler = MockHTTPSHandler(
        302, location="https://api.github.com/repos/SGGlobal/QLA/other"
    )
    opener = _opener_with(handler)
    transport = ProductionHTTPTransport(opener=opener)

    with pytest.raises(CredentialSafeError):
        transport(
            "GET",
            "https://api.github.com/repos/SGGlobal/QLA",
            {"Authorization": "Bearer secret-token"},
            5,
        )

    assert len(handler.requests) == 1


def test_only_initial_request_occurs():
    handler = MockHTTPSHandler(301, location="https://api.github.com/elsewhere")
    opener = _opener_with(handler)
    transport = ProductionHTTPTransport(opener=opener)

    with pytest.raises(CredentialSafeError):
        transport(
            "GET", "https://api.github.com/repos/SGGlobal/QLA", {}, 5
        )

    assert len(handler.requests) == 1


def test_credentials_not_forwarded():
    handler = MockHTTPSHandler(302, location="https://evil.example/steal")
    opener = _opener_with(handler)
    transport = ProductionHTTPTransport(opener=opener)

    with pytest.raises(CredentialSafeError):
        transport(
            "GET",
            "https://api.github.com/repos/SGGlobal/QLA",
            {"Authorization": "Bearer secret-token"},
            5,
        )

    assert len(handler.requests) == 1


def test_redirect_error_does_not_reveal_credentials():
    handler = MockHTTPSHandler(302, location="https://evil.example/steal")
    opener = _opener_with(handler)
    transport = ProductionHTTPTransport(opener=opener)

    try:
        transport(
            "GET",
            "https://api.github.com/repos/SGGlobal/QLA",
            {"Authorization": "Bearer secret-token"},
            5,
        )
    except CredentialSafeError as exc:
        assert "secret-token" not in str(exc)
        assert "Bearer" not in str(exc)
    else:
        raise AssertionError("expected redirect rejection")


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_error_status_retained(status):
    """
    Non-2xx responses are returned by the mock so HTTPErrorProcessor
    and HTTPDefaultErrorHandler run. The transport preserves the status.
    """
    handler = MockHTTPSHandler(status)
    opener = _opener_with(handler)
    transport = ProductionHTTPTransport(opener=opener)

    response = transport(
        "GET", "https://api.github.com/repos/SGGlobal/QLA", {}, 5
    )

    assert response.status == status
    assert response.body == b""
    assert len(handler.requests) == 1


def test_secure_opener_chain():
    opener = build_secure_opener()
    handler_types = {type(handler).__name__ for handler in opener.handlers}

    assert "HTTPSHandler" in handler_types
    assert "RedirectRejectingHandler" in handler_types
    assert "HTTPDefaultErrorHandler" in handler_types
    assert "HTTPErrorProcessor" in handler_types
    assert "HTTPRedirectHandler" not in handler_types


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
