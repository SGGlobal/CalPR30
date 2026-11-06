"""
SKIPPY AI Collaboration Bridge — Production HTTPS Transport.

Read-only. Fixed host. No network access on import.

Compatible with GitHubLiveProvider's HTTPTransport interface:
    transport(method, url, headers, timeout) -> HTTPResponse
"""

from __future__ import annotations

import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPSHandler

from skippy.collaboration.github_live_provider import (
    GITHUB_API_HOST,
    HTTPResponse,
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    CredentialSafeError,
)


ALLOWED_HOST = "api.github.com"


class RedirectRejectingHandler(HTTPSHandler):
    """
    HTTPS handler that refuses every redirect before a second request.

    Authorization headers are never forwarded because the redirect is
    rejected rather than followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CredentialSafeError("Unexpected redirect")


def _validate_url(method: str, url: str) -> None:
    if not isinstance(method, str) or method.upper() != "GET":
        raise CredentialSafeError("Unsupported HTTP method")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise CredentialSafeError("HTTPS required")
    if parsed.username or parsed.password:
        raise CredentialSafeError("URL userinfo is not permitted")
    if parsed.port not in {None, 443}:
        raise CredentialSafeError("Unexpected port")
    if parsed.hostname != ALLOWED_HOST:
        raise CredentialSafeError("Unexpected API host")
    if not url.startswith(GITHUB_API_HOST):
        raise CredentialSafeError("Unexpected API host")


class ProductionHTTPTransport:
    """
    HTTPS-only transport for GitHub's documented API host.

    GET only. TLS validation enabled. Redirects rejected before following.
    No retries. Response body is streamed with an enforced size limit.
    """

    def __init__(self, opener=None):
        # opener is injectable so tests never touch the network.
        self._opener = opener

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout: int,
    ) -> HTTPResponse:
        _validate_url(method, url)

        effective_timeout = min(timeout, REQUEST_TIMEOUT_SECONDS)

        request = Request(url, method="GET")
        for key, value in headers.items():
            request.add_header(key, value)

        opener = self._opener or build_opener(
            RedirectRejectingHandler(context=ssl.create_default_context())
        )

        try:
            with opener.open(request, timeout=effective_timeout) as response:
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
        except HTTPError as exc:
            # Preserve the status so the provider can classify it.
            # Do not include the body or any header in the message.
            return HTTPResponse(status=exc.code, body=b"", headers={})
        except TimeoutError:
            raise CredentialSafeError("GitHub request timed out") from None
        except URLError:
            raise CredentialSafeError("GitHub request failed") from None
        except Exception:
            raise CredentialSafeError("GitHub request failed") from None


def production_transport():
    """
    Factory. Does not perform network access.
    """

    return ProductionHTTPTransport()
