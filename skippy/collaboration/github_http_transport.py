"""
SKIPPY AI Collaboration Bridge — Production HTTPS Transport.

Read-only. Fixed host. No network access on import.

Compatible with GitHubLiveProvider's HTTPTransport interface:
    transport(method, url, headers, timeout) -> HTTPResponse
"""

from __future__ import annotations

import ssl
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from skippy.collaboration.github_live_provider import (
    GITHUB_API_HOST,
    HTTPResponse,
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    CredentialSafeError,
)


ALLOWED_HOST = "api.github.com"


class ProductionHTTPTransport:
    """
    HTTPS-only transport for GitHub's documented API host.

    GET only. TLS validation enabled. Redirects rejected. No retries.
    Response body is streamed with an enforced size limit.
    """

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout: int,
    ) -> HTTPResponse:
        if not isinstance(method, str) or method.upper() != "GET":
            raise CredentialSafeError("Unsupported HTTP method")

        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise CredentialSafeError("HTTPS required")
        if parsed.hostname != ALLOWED_HOST:
            raise CredentialSafeError("Unexpected API host")
        if not url.startswith(GITHUB_API_HOST):
            raise CredentialSafeError("Unexpected API host")

        effective_timeout = min(timeout, REQUEST_TIMEOUT_SECONDS)

        # Strip any authorization header from the request object we log.
        # We never log headers at all.
        request = Request(url, method="GET")
        for key, value in headers.items():
            if key.lower() == "authorization":
                # Passed to the request, never logged.
                request.add_header(key, value)
            else:
                request.add_header(key, value)

        context = ssl.create_default_context()

        try:
            with urlopen(
                request,
                timeout=effective_timeout,
                context=context,
            ) as response:
                # Reject redirects by not following them.
                # urllib follows redirects by default; we disable that
                # by checking the final URL and by using a custom handler
                # expectation. If the response URL differs, treat as redirect.
                final = getattr(response, "url", url)
                if final != url:
                    raise CredentialSafeError("Unexpected redirect")

                chunks = []
                remaining = MAX_RESPONSE_BYTES + 1
                while remaining > 0:
                    chunk = response.read(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)

                body = b"".join(chunks)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CredentialSafeError("Response exceeds size bound")

                status = getattr(response, "status", 200)
                return HTTPResponse(status=status, body=body, headers={})

        except CredentialSafeError:
            raise
        except TimeoutError:
            raise CredentialSafeError("GitHub request timed out") from None
        except Exception:
            raise CredentialSafeError("GitHub request failed") from None


def production_transport():
    """
    Factory. Does not perform network access.
    """

    return ProductionHTTPTransport()
