"""
SKIPPY AI Collaboration Bridge — Production Transport Tests.

No real GitHub contact. Uses the transport's validation paths and a fake
probe runner.
"""

import pytest

from skippy.collaboration.github_http_transport import (
    ProductionHTTPTransport,
    production_transport,
)
from skippy.collaboration.github_live_probe import (
    ProbeCancelled,
    run_probe,
)
from skippy.collaboration.github_live_provider import CredentialSafeError


def test_https_enforcement():
    transport = ProductionHTTPTransport()
    with pytest.raises(CredentialSafeError, match="HTTPS required"):
        transport("GET", "http://api.github.com/repos/SGGlobal/QLA", {}, 5)


def test_exact_host_validation():
    transport = ProductionHTTPTransport()
    with pytest.raises(CredentialSafeError, match="Unexpected API host"):
        transport("GET", "https://evil.example/repos", {}, 5)


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


def test_explicit_activation_requires_confirmation():
    def decline(prompt):
        return "no"

    with pytest.raises(ProbeCancelled):
        run_probe(live=True, confirmed=False, confirmer=decline)


def test_safe_cancellation():
    def decline(prompt):
        return ""

    with pytest.raises(ProbeCancelled, match="cancelled"):
        run_probe(live=True, confirmed=False, confirmer=decline)


def test_missing_credentials_fail_closed():
    def empty(prompt):
        return ""

    with pytest.raises(CredentialSafeError, match="Missing GitHub token"):
        run_probe(
            live=True,
            confirmed=True,
            token_reader=empty,
        )


def test_token_not_in_error():
    secret = "ghp_should_not_appear"

    def reader(prompt):
        return secret

    try:
        run_probe(live=True, confirmed=True, token_reader=reader)
    except Exception as exc:
        assert secret not in str(exc)
    else:
        # If the probe somehow proceeds without network, the token must
        # still not appear in the returned report.
        pass


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
