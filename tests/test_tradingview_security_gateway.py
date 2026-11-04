"""
SKIPPY AI TRADER — PHASE 8

Local Webhook Security Gateway Tests.
"""

import json

import pytest

from skippy.adapters.tradingview.security_gateway import (
    WebhookSecurityError,
    WebhookSecurityGateway,
)


TEST_SECRET = "local-simulation-secret-32-characters-minimum"


def make_body():
    return json.dumps({
        "symbol": "BTCUSDT",
        "action": "BUY",
        "price": 65000,
        "timestamp": "2026-11-04T18:00:00Z",
    }).encode("utf-8")


def test_valid_request_accepted():
    gateway = WebhookSecurityGateway(TEST_SECRET)

    payload = gateway.validate(make_body(), TEST_SECRET)

    assert payload["symbol"] == "BTCUSDT"


def test_invalid_token_rejected():
    gateway = WebhookSecurityGateway(TEST_SECRET)

    with pytest.raises(WebhookSecurityError):
        gateway.validate(make_body(), "incorrect-token")


def test_missing_token_rejected():
    gateway = WebhookSecurityGateway(TEST_SECRET)

    with pytest.raises(WebhookSecurityError):
        gateway.validate(make_body(), "")


def test_oversized_payload_rejected():
    gateway = WebhookSecurityGateway(
        TEST_SECRET,
        max_payload_bytes=10,
    )

    with pytest.raises(WebhookSecurityError):
        gateway.validate(make_body(), TEST_SECRET)


def test_invalid_json_rejected():
    gateway = WebhookSecurityGateway(TEST_SECRET)

    with pytest.raises(WebhookSecurityError):
        gateway.validate(b"not-json", TEST_SECRET)


def test_json_array_rejected():
    gateway = WebhookSecurityGateway(TEST_SECRET)

    with pytest.raises(WebhookSecurityError):
        gateway.validate(b"[]", TEST_SECRET)


def test_non_bytes_body_rejected():
    gateway = WebhookSecurityGateway(TEST_SECRET)

    with pytest.raises(WebhookSecurityError):
        gateway.validate("not-bytes", TEST_SECRET)


def test_short_secret_rejected():
    with pytest.raises(ValueError):
        WebhookSecurityGateway("short")


def test_invalid_payload_limit_rejected():
    with pytest.raises(ValueError):
        WebhookSecurityGateway(
            TEST_SECRET,
            max_payload_bytes=0,
        )