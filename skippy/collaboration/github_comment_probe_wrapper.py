"""Offline-tested credential seam for a future one-page observation probe.

No live factory, credential loader, environment access or admission. Callers must
supply a transport and token; this implementation is authorized for fakes only.
A new explicit invocation creates a new one-request budget. This is not durable
replay protection, an authorization system, or a secret-erasure guarantee.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from skippy.collaboration.github_issue_comment_reader import read_comment_page
from skippy.collaboration.github_live_provider import HTTPResponse

execution_enabled = False
PROBE_URL = 'https://api.github.com/repos/SGGlobal/QLA/issues/23/comments?per_page=25&page=1'


class ProbeRefusal(Exception):
    """Static reason and actual delegated invocation count; no foreign text."""

    def __init__(self, reason: str, calls: int, status: int | None = None):
        super().__init__(reason)
        self.calls = calls
        self.status = status


@dataclass(frozen=True)
class ProbeSummary:
    status: int
    transport_calls: int
    body_bytes: int
    body_sha256: str
    comment_count: int
    minimum_id: int | None
    maximum_id: int | None
    pagination: str
    next_token: str | None


def observe_issue23_once(transport, token: str) -> ProbeSummary:
    """Strictly read issue23/page1 through a supplied capability, once at most.

    Token syntax is bounded ASCII opaque text, not verification of permissions,
    validity, owner or expiry. Only synthetic tokens are used by the tests.
    The token is used only in this invocation and never included in the result.
    Python locals/debuggers and an untrusted delegate can still see the token.
    """
    if execution_enabled is not False:
        raise ProbeRefusal('execution disabled required', 0)
    if not callable(transport):
        raise ProbeRefusal('transport refused', 0)
    if type(token) is not str or re.fullmatch(r'[A-Za-z0-9_]{1,512}', token, re.ASCII) is None:
        raise ProbeRefusal('credential syntax refused', 0)
    calls = 0
    status = None

    def credential_transport(method, url, headers, timeout):
        nonlocal calls, status
        if calls != 0:
            raise ProbeRefusal('request budget exhausted', calls, status)
        if (type(method) is not str or method != 'GET' or
                type(url) is not str or url != PROBE_URL or
                type(headers) is not dict or headers != {'Accept': 'application/vnd.github+json'} or
                type(timeout) is not int or timeout != 10):
            raise ProbeRefusal('request refused', calls, status)
        # Charge before invocation, including timeout and unknown remote outcomes.
        calls += 1
        response = transport('GET', PROBE_URL, {
            'Accept': 'application/vnd.github+json',
            'Authorization': 'Bearer ' + token,
        }, 10)
        if type(response) is HTTPResponse and type(response.status) is int and 100 <= response.status <= 599:
            status = response.status
        return response

    try:
        page = read_comment_page(credential_transport, 'SGGlobal/QLA', 23, strict_response=True)
        # The unchanged strict reader has already validated UTF-8 JSON, page shape,
        # unique positive ids, byte limits and canonical pagination. Never return
        # raw body, foreign metadata or a request header in this evidence object.
        payload = json.loads(page.body)
        ids = [comment['id'] for comment in payload]
        return ProbeSummary(
            status=200, transport_calls=calls, body_bytes=len(page.body),
            body_sha256=hashlib.sha256(page.body).hexdigest(), comment_count=len(ids),
            minimum_id=min(ids) if ids else None, maximum_id=max(ids) if ids else None,
            pagination=page.pagination, next_token=page.next_token,
        )
    except Exception:
        raise ProbeRefusal('probe refused', calls, status) from None
    except (KeyboardInterrupt, SystemExit):
        # Preserve cancellation semantics, suppress foreign exception text.
        raise ProbeRefusal('probe interrupted', calls, status) from None
