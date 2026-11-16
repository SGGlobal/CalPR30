"""Offline-only bounded observation session over an injected test transport.

No credentials, live factory, persistence, admission or snapshot guarantees.
Observation bodies are untrusted data and are excluded from result reprs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from skippy.collaboration.github_issue_comment_reader import ReaderRefusal, read_comment_page

execution_enabled = False
MAX_CALLS = 4
REPOSITORY = "SGGlobal/QLA"
THREADS = (23, 26)
_VALIDATION_CODES = frozenset({
    "TRANSPORT", "RESPONSE", "BODY_BOUNDS", "CONTENT_TYPE", "JSON",
    "PAGE_STRUCTURE", "PAGINATION",
})


@dataclass(frozen=True)
class CommentObservation:
    repository: str
    thread: int
    comment_id: int
    body: str | None = field(repr=False)


@dataclass(frozen=True)
class SessionEvidence:
    outcome: str
    transport_calls: int
    pages_validated: int
    validated_bytes: int
    entries_observed: int
    unique_comments: int
    duplicates: int
    next_hint: str | None = None
    validation_code: str = "UNCLASSIFIED"
    coverage: str = "PARTIAL_OR_UNKNOWN"


@dataclass(frozen=True)
class SessionResult:
    evidence: SessionEvidence
    # None on any failure/conflict/cancellation; () is a successful empty batch.
    observations: tuple[CommentObservation, ...] | None = field(repr=False)


def read_comment_session(transport, repository: str, number: int,
                         *, start_token: str | None = None) -> SessionResult:
    """Read up to four pages using fakes only, returning untrusted observations.

    A new explicit invocation has a new budget. Hints are unauthenticated routing
    metadata, not durable cursors. Successful results may still omit comments.
    Evidence is safe to display; observations are an explicit in-memory channel.
    No function here authorizes a supplied capability to access a real service.
    """
    calls = pages = size = entries = duplicates = 0
    observed: dict[int, CommentObservation] = {}

    def finish(outcome, *, release=False, next_hint=None, validation_code="UNCLASSIFIED"):
        code = (validation_code if type(validation_code) is str and
                validation_code in _VALIDATION_CODES else "UNCLASSIFIED")
        return SessionResult(
            SessionEvidence(outcome, calls, pages, size, entries, len(observed), duplicates,
                            next_hint, code),
            tuple(observed.values()) if release else None,
        )

    if (execution_enabled is not False or type(repository) is not str or
            repository != REPOSITORY or type(number) is not int or number not in THREADS or
            not callable(transport)):
        return finish("REFUSED")

    token = start_token
    try:
        while pages < MAX_CALLS:
            expected = token if token is not None else (
                f"https://api.github.com/repos/{REPOSITORY}/issues/{number}/comments"
                "?per_page=25&page=1"
            )
            before = calls

            def bounded_transport(method, url, headers, timeout):
                nonlocal calls
                if (calls >= MAX_CALLS or calls != before or
                        type(method) is not str or method != "GET" or
                        type(url) is not str or url != expected or
                        type(headers) is not dict or
                        headers != {"Accept": "application/vnd.github+json"} or
                        type(timeout) is not int or timeout != 10):
                    raise ValueError("session request refused")
                # Charge immediately before delegation, even for unknown outcomes.
                calls += 1
                return transport(method, url, headers, timeout)

            # The unchanged reader validates even caller-supplied start tokens.
            page = read_comment_page(bounded_transport, repository, number,
                                     page_token=token, strict_response=True)
            pages += 1
            size += len(page.body)
            payload = json.loads(page.body)
            entries += len(payload)
            for comment in payload:
                comment_id, body = comment["id"], comment["body"]
                if comment_id in observed:
                    if observed[comment_id].body != body:
                        return finish("CONFLICT")
                    duplicates += 1
                else:
                    observed[comment_id] = CommentObservation(repository, number, comment_id, body)
            token = page.next_token
            if token is None:
                return finish("NO_NEXT_HINT", release=True)
        return finish("BUDGET_STOPPED", release=True, next_hint=token)
    except ReaderRefusal as error:
        code = error.validation_code if type(error) is ReaderRefusal else "UNCLASSIFIED"
        return finish("REFUSED" if calls == 0 else "UNCERTAIN", validation_code=code)
    except (KeyboardInterrupt, SystemExit):
        return finish("INTERRUPTED")
    except Exception:
        return finish("UNCERTAIN")
