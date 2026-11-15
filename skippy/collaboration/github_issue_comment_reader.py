"""T2 offline reader seam. Injected transport only; no credentials or live factory.

One bounded GET per call. Raw bytes are observations, never authenticated work.
Missing pagination headers mean UNKNOWN. Strict response validation is opt-in;
no completeness claim is made.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from skippy.collaboration.github_live_provider import HTTPResponse

execution_enabled = False
REPOSITORY = "SGGlobal/QLA"
THREADS = (23, 26)
MAX_BYTES = 1_000_000
MAX_LINK_BYTES = 8192
PER_PAGE = 25
TIMEOUT = 10


class ReaderRefusal(Exception):
    """Static classification with actual transport invocation count; no remote text."""

    def __init__(self, reason: str, calls: int, *, validation_code: str = "UNCLASSIFIED"):
        super().__init__(reason)
        self.calls = calls
        self.validation_code = validation_code


@dataclass(frozen=True)
class CommentPageBytes:
    repository: str
    number: int
    body: bytes
    next_token: str | None
    pagination: str
    transport_calls: int = 1


def _base(number: int) -> str:
    return f"https://api.github.com/repos/{REPOSITORY}/issues/{number}/comments"


def _page(url: str, number: int) -> int:
    # Canonical narrow grammar: no userinfo, fragments, alternate ports, extra
    # query keys, percent escapes, lookalike origins or cross-thread endpoints.
    match = re.fullmatch(re.escape(_base(number)) + r"\?per_page=25&page=([1-9][0-9]{0,3})", url)
    if match is None:
        raise ValueError("url")
    return int(match.group(1))


def _links(headers: dict, number: int, page: int) -> str | None:
    if type(headers) is not dict:
        raise ValueError("headers")
    links = []
    for key, value in headers.items():
        if type(key) is not str or type(value) is not str:
            raise ValueError("headers")
        if key.lower() == "link":
            links.append(value)
    if not links:
        return None
    if len(links) != 1 or len(links[0].encode("utf-8")) > MAX_LINK_BYTES:
        raise ValueError("link")
    seen = {}
    for item in links[0].split(","):
        match = re.fullmatch(r' *<([^<>\s]+)>; rel="(next|prev|first|last)" *', item)
        if match is None:
            raise ValueError("link")
        url, relation = match.groups()
        target = _page(url, number)
        if relation in seen:
            raise ValueError("link")
        if relation == "next" and target != page + 1:
            raise ValueError("next")
        if relation == "prev" and target != page - 1:
            raise ValueError("prev")
        if relation == "first" and target != 1:
            raise ValueError("first")
        if relation == "last" and target < page:
            raise ValueError("last")
        seen[relation] = url
    if "next" in seen and "last" in seen:
        if _page(seen["last"], number) < _page(seen["next"], number):
            raise ValueError("contradictory pagination")
    return seen.get("next")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-JSON constant")


def _strict_content_type(headers):
    if type(headers) is not dict:
        raise ValueError("headers")
    values = []
    size = 0
    for name, value in headers.items():
        if type(name) is not str or type(value) is not str:
            raise ValueError("headers")
        if name.lower() not in ("content-type", "link"):
            continue
        size += len(name.lower()) + len(value.encode("utf-8"))
        if size > 16384 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
            raise ValueError("headers")
        if name.lower() == "content-type":
            values.append(value)
    if len(values) != 1 or re.fullmatch(
        r' *(?:application/json|application/vnd\.github\+json) *'
        r'(?:; *charset *= *(?:utf-8|"utf-8") *)?', values[0], re.IGNORECASE | re.ASCII,
    ) is None:
        raise ValueError("content-type")


def read_comment_page(transport, repository: str, number: int,
                      page_token: str | None = None, *, strict_response: bool = False) -> CommentPageBytes:
    """Invoke a supplied test transport once; never follow a Link automatically.

    A caller may explicitly pass a validated next URL for another manual call.
    Tokens are bounded routing hints, not authenticated cursors or snapshots.
    Only the exact HTTPResponse type is supported. No token parameter exists.
    """
    if execution_enabled is not False:
        raise ReaderRefusal("execution disabled required", 0)
    if type(strict_response) is not bool:
        raise ReaderRefusal("strict option refused", 0)
    if type(repository) is not str or repository != REPOSITORY:
        raise ReaderRefusal("repository refused", 0)
    if type(number) is not int or number not in THREADS:
        raise ReaderRefusal("thread refused", 0)
    if not callable(transport):
        raise ReaderRefusal("transport refused", 0)
    url = _base(number) + "?per_page=25&page=1"
    if page_token is not None:
        if type(page_token) is not str or len(page_token) > 256:
            raise ReaderRefusal("page token refused", 0)
        url = page_token
    try:
        page = _page(url, number)
    except ValueError:
        raise ReaderRefusal("page token refused", 0) from None
    try:
        response = transport("GET", url, {"Accept": "application/vnd.github+json"}, TIMEOUT)
    except Exception:
        raise ReaderRefusal("transport failed", 1, validation_code="TRANSPORT") from None
    # Interrupts propagate; there is no retry, persisted work or resume path.
    if type(response) is not HTTPResponse or type(response.status) is not int or response.status != 200:
        raise ReaderRefusal("response refused", 1, validation_code="RESPONSE")
    if type(response.body) is not bytes or len(response.body) > MAX_BYTES:
        raise ReaderRefusal("body refused", 1, validation_code="BODY_BOUNDS")
    validation_code = "CONTENT_TYPE"
    try:
        if strict_response:
            _strict_content_type(response.headers)
        validation_code = "JSON"
        payload = json.loads(response.body.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=_constant)
        validation_code = "PAGE_STRUCTURE"
        if type(payload) is not list or len(payload) > PER_PAGE:
            raise ValueError("page")
        ids = set()
        for item in payload:
            if type(item) is not dict or type(item.get("id")) is not int or item["id"] <= 0:
                raise ValueError("comment")
            if "body" not in item or (item["body"] is not None and type(item["body"]) is not str):
                raise ValueError("comment")
            if item["id"] in ids:
                raise ValueError("duplicate comment")
            ids.add(item["id"])
        validation_code = "PAGINATION"
        next_token = _links(response.headers, number, page)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ReaderRefusal("page refused", 1, validation_code=validation_code) from None
    return CommentPageBytes(repository, number, response.body, next_token,
                            "NEXT_OBSERVED" if next_token else "UNKNOWN")

