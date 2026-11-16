"""Synthetic capabilities only; no credentials or project-service connections."""
import ast
import dataclasses
import importlib
import io
import json
import pathlib
import socket
from email.message import Message

import pytest

from skippy.collaboration import github_comment_read_session as session
from skippy.collaboration import github_http_transport as https
from skippy.collaboration import github_issue_comment_reader as reader
from skippy.collaboration.github_live_provider import HTTPResponse

SECRET = "synthetic_secret_and_untrusted_comment"


def url(page, thread=23):
    return f"https://api.github.com/repos/SGGlobal/QLA/issues/{thread}/comments?per_page=25&page={page}"


def response(items=(), next_page=None, *, thread=23, raw=None, content_type="application/json"):
    headers = {"content-type": content_type}
    if next_page is not None:
        headers["link"] = f'<{url(next_page, thread)}>; rel="next"'
    body = raw if raw is not None else json.dumps(list(items), ensure_ascii=True).encode()
    return HTTPResponse(200, body, headers)


def comment(comment_id, body="observation"):
    return {"id": comment_id, "body": body}


class Fake:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if len(self.calls) > len(self.responses):
            raise AssertionError("unplanned call")
        item = self.responses[len(self.calls) - 1]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("production networking/factory or protected component invoked")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(https, "production_transport", forbidden)


def run(fake, thread=23, **kwargs):
    result = session.read_comment_session(fake, "SGGlobal/QLA", thread, **kwargs)
    assert result.evidence.coverage == "PARTIAL_OR_UNKNOWN"
    assert result.evidence.transport_calls == len(fake.calls)
    assert len(fake.calls) <= 4
    return result


def assert_calls(fake, pages, thread=23):
    assert fake.calls == [("GET", url(p, thread),
                           {"Accept": "application/vnd.github+json"}, 10) for p in pages]


def test_one_page_body_identity_and_safe_summary(capsys):
    body = SECRET + "\nΩ\x00e\u0301"
    fake = Fake([response([comment(1, body), comment(2, None), comment(3, "")])])
    result = run(fake)
    assert result.evidence.outcome == "NO_NEXT_HINT"
    assert result.evidence.pages_validated == 1
    assert result.evidence.entries_observed == result.evidence.unique_comments == 3
    assert result.evidence.next_hint is None
    assert [(o.repository, o.thread, o.comment_id, o.body) for o in result.observations] == [
        ("SGGlobal/QLA", 23, 1, body), ("SGGlobal/QLA", 23, 2, None),
        ("SGGlobal/QLA", 23, 3, "")]
    assert SECRET not in repr(result) + repr(result.observations)
    assert SECRET not in json.dumps(dataclasses.asdict(result.evidence))
    assert capsys.readouterr().out == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.observations[0].body = "changed"
    assert_calls(fake, [1])


@pytest.mark.parametrize("thread", [23, 26])
@pytest.mark.parametrize("more", [False, True])
def test_four_pages_with_or_without_further_hint(thread, more):
    responses = [response([comment(p)], p + 1 if p < 4 or more else None, thread=thread)
                 for p in range(1, 5)]
    fake = Fake(responses + [response([comment(999)])])
    result = run(fake, thread)
    assert result.evidence.outcome == ("BUDGET_STOPPED" if more else "NO_NEXT_HINT")
    assert result.evidence.next_hint == (url(5, thread) if more else None)
    assert result.evidence.pages_validated == 4
    assert result.evidence.validated_bytes == sum(len(r.body) for r in responses)
    assert [o.comment_id for o in result.observations] == [1, 2, 3, 4]
    assert_calls(fake, [1, 2, 3, 4], thread)


def test_100_entry_limit_and_four_million_byte_boundary():
    responses = []
    for page in range(4):
        items = [comment(page * 25 + n) for n in range(1, 26)]
        raw = json.dumps(items).encode()
        raw += b" " * (reader.MAX_BYTES - len(raw))
        responses.append(response(next_page=page + 2, raw=raw))
    fake = Fake(responses)
    result = run(fake)
    assert result.evidence.validated_bytes == 4_000_000
    assert result.evidence.entries_observed == result.evidence.unique_comments == 100
    assert len(result.observations) == 100
    assert result.evidence.outcome == "BUDGET_STOPPED"


def test_same_body_deduplicates_without_unicode_normalization():
    fake = Fake([response([comment(3, "é"), comment(1, None)], 2),
                 response([comment(3, "é"), comment(2, "e\u0301"), comment(1, None)])])
    result = run(fake)
    assert result.evidence.entries_observed == 5
    assert result.evidence.unique_comments == 3 and result.evidence.duplicates == 2
    assert [(o.comment_id, o.body) for o in result.observations] == [(3, "é"), (1, None), (2, "e\u0301")]
    assert_calls(fake, [1, 2])


@pytest.mark.parametrize("before,after", [("a", "b"), (None, ""), ("é", "e\u0301"),
                                         (SECRET, "changed")])
def test_conflict_withholds_entire_batch(before, after):
    fake = Fake([response([comment(1, before)], 2), response([comment(1, after)], 3),
                 response([comment(2)])])
    result = run(fake)
    assert result.evidence.outcome == "CONFLICT"
    assert result.observations is None and result.evidence.next_hint is None
    assert result.evidence.pages_validated == 2
    assert SECRET not in repr(result) + repr(result.evidence)
    assert_calls(fake, [1, 2])


@pytest.mark.parametrize("empty_body", [b"[]", b"[]  "])
def test_empty_page_with_next_does_not_infer_end(empty_body):
    fake = Fake([response(raw=empty_body, next_page=2), response([comment(1)])])
    result = run(fake)
    assert result.evidence.entries_observed == 1
    assert [o.comment_id for o in result.observations] == [1]
    assert_calls(fake, [1, 2])


def test_successful_empty_batch_is_distinct_from_failure():
    result = run(Fake([response()]))
    assert result.observations == ()
    assert result.evidence.outcome == "NO_NEXT_HINT"


def test_explicit_start_hint_and_new_session_budget():
    fake = Fake([response([comment(p)], p + 1) for p in range(5, 9)])
    result = run(fake, start_token=url(5))
    assert_calls(fake, [5, 6, 7, 8])
    assert result.evidence.next_hint == url(9)
    second = Fake([response([comment(9)])])
    assert run(second, start_token=result.evidence.next_hint).evidence.transport_calls == 1
    assert_calls(second, [9])


@pytest.mark.parametrize("token", [False, 1, b"url", "", "x" * 257, url(0), url("01"),
    url(10000), url(2, 26), url(2).replace("SGGlobal/QLA", "SGGlobal/other"),
    url(2).replace("api.github.com", "evil.invalid"), url(2).replace("https:", "http:"),
    url(2).replace("api.github.com", "u:p@api.github.com"), url(2) + "#fragment",
    url(2) + "&page=3", url(2).replace("per_page=25&page=2", "page=2&per_page=25"),
    url(2).replace("/repos/SGGlobal/QLA", "/repositories/1448604243")])
def test_invalid_start_hint_refuses_without_delegation(token):
    fake = Fake([])
    result = run(fake, start_token=token)
    assert result.evidence.outcome == "REFUSED" and result.observations is None
    assert fake.calls == []


@pytest.mark.parametrize("repository,thread", [("other/QLA", 23), (None, 23),
    ("SGGlobal/QLA", True), ("SGGlobal/QLA", "23"), ("SGGlobal/QLA", 24)])
def test_repository_thread_exact_types(repository, thread):
    fake = Fake([])
    result = session.read_comment_session(fake, repository, thread)
    assert result.evidence.outcome == "REFUSED" and result.evidence.transport_calls == 0
    assert result.observations is None and fake.calls == []


def test_noncallable_and_execution_flag_refused(monkeypatch):
    assert session.read_comment_session(None, "SGGlobal/QLA", 23).evidence.outcome == "REFUSED"
    monkeypatch.setattr(session, "execution_enabled", True)
    fake = Fake([])
    assert run(fake).evidence.outcome == "REFUSED"


@pytest.mark.parametrize("problem,code", [
    (HTTPResponse(403, b"", {}), "RESPONSE"),
    (response(raw=b"invalid " + SECRET.encode()), "JSON"),
    (response(content_type="text/html"), "CONTENT_TYPE"),
    (HTTPResponse(200, b"[]", {}), "CONTENT_TYPE"),
    (HTTPResponse(200, b"[]", {"content-type": "application/json", "Content-Type": "application/json"}), "CONTENT_TYPE"),
    (response(raw=b'[{"id":1,"id":2,"body":"x"}]'), "JSON"),
    (response(raw=b'[NaN]'), "JSON"),
    (response([comment(1), comment(1)]), "PAGE_STRUCTURE"),
    (response(raw=b" " * (reader.MAX_BYTES + 1)), "BODY_BOUNDS"),
    (response(next_page=2), "PAGINATION"),  # page2 incorrectly links to itself
])
def test_later_validation_failure_withholds_partial_batch(problem, code):
    fake = Fake([response([comment(1, SECRET)], 2), problem, response([comment(999)])])
    result = run(fake)
    assert result.observations is None and result.evidence.outcome == "UNCERTAIN"
    assert result.evidence.pages_validated == 1 and result.evidence.entries_observed == 1
    assert result.evidence.validation_code == code and result.evidence.next_hint is None
    assert SECRET not in repr(result)
    assert_calls(fake, [1, 2])


@pytest.mark.parametrize("target", [url(1), url(4), url(3, 26),
    url(3).replace("api.github.com", "evil.invalid")])
def test_cycle_skip_and_cross_thread_links_stop(target):
    bad = HTTPResponse(200, b'[]', {"content-type": "application/json",
                                   "link": f'<{target}>; rel="next"'})
    fake = Fake([response([comment(1)], 2), bad])
    result = run(fake)
    assert result.evidence.validation_code == "PAGINATION" and result.observations is None
    assert_calls(fake, [1, 2])


def test_numeric_reordered_response_link_normalizes_before_follow():
    first = HTTPResponse(200, b'[]', {"content-type": "application/json",
        "link": '<https://api.github.com/repositories/1448604243/issues/23/comments?page=2&per_page=25>; rel="next"'})
    fake = Fake([first, response([comment(2)])])
    result = run(fake)
    assert result.evidence.outcome == "NO_NEXT_HINT"
    assert_calls(fake, [1, 2])


@pytest.mark.parametrize("error,outcome", [(TimeoutError(SECRET), "UNCERTAIN"),
    (RuntimeError(SECRET), "UNCERTAIN"), (KeyboardInterrupt(SECRET), "INTERRUPTED"),
    (SystemExit(SECRET), "INTERRUPTED")])
@pytest.mark.parametrize("failure_page", [1, 3, 4])
def test_failures_and_cancellation_charge_before_delegation(error, outcome, failure_page, capsys):
    fake = Fake([response([comment(p, SECRET)], p + 1) for p in range(1, failure_page)] +
                [error, response([comment(999)])])
    result = run(fake)
    assert result.evidence.outcome == outcome and result.observations is None
    assert result.evidence.pages_validated == failure_page - 1
    assert result.evidence.next_hint is None
    assert SECRET not in repr(result) + json.dumps(dataclasses.asdict(result.evidence))
    assert capsys.readouterr().out == "" and capsys.readouterr().err == ""
    assert_calls(fake, list(range(1, failure_page + 1)))


def test_reader_always_receives_strict_mode(monkeypatch):
    actual = session.read_comment_page
    strict_flags = []
    def spy(*args, **kwargs):
        strict_flags.append(kwargs["strict_response"])
        return actual(*args, **kwargs)
    monkeypatch.setattr(session, "read_comment_page", spy)
    fake = Fake([response(next_page=2), response()])
    run(fake)
    assert strict_flags == [True, True]


@pytest.mark.parametrize("code", [SECRET, None, ["JSON"]])
def test_unrecognized_reader_codes_do_not_escape(monkeypatch, code):
    def bad(*args, **kwargs):
        raise reader.ReaderRefusal(SECRET, 999, validation_code=code)
    monkeypatch.setattr(session, "read_comment_page", bad)
    result = run(Fake([]))
    assert result.evidence.validation_code == "UNCLASSIFIED"
    assert result.evidence.transport_calls == 0 and SECRET not in repr(result)


def test_unexpected_internal_failure_withholds_batch(monkeypatch):
    def bad(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(session, "read_comment_page", bad)
    result = run(Fake([]))
    assert result.evidence.outcome == "UNCERTAIN" and result.observations is None
    assert SECRET not in repr(result)


def test_budget_guard_charges_and_rejects_reader_double_delegation(monkeypatch):
    def bad(transport, repository, thread, **kwargs):
        transport("GET", url(1), {"Accept": "application/vnd.github+json"}, 10)
        transport("GET", url(1), {"Accept": "application/vnd.github+json"}, 10)
    monkeypatch.setattr(session, "read_comment_page", bad)
    fake = Fake([response()])
    result = run(fake)
    assert result.evidence.transport_calls == 1 and result.observations is None


@pytest.mark.parametrize("args", [("POST", url(1), {"Accept": "application/vnd.github+json"}, 10),
    ("GET", url(2), {"Accept": "application/vnd.github+json"}, 10),
    ("GET", url(1), {"Authorization": SECRET}, 10),
    ("GET", url(1), {"Accept": "application/vnd.github+json"}, 10.0)])
def test_bad_reader_delegate_request_never_reaches_transport(monkeypatch, args):
    def bad(transport, *a, **kwargs):
        transport(*args)
    monkeypatch.setattr(session, "read_comment_page", bad)
    result = run(Fake([]))
    assert result.observations is None and result.evidence.transport_calls == 0
    assert SECRET not in repr(result)


def test_offline_import_and_no_operational_imports(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected environment/credential access")
    # Reload while both network and environment lookups are blocked.
    with monkeypatch.context() as patch:
        import os
        patch.setattr(os.environ, "get", forbidden)
        importlib.reload(session)
    source = pathlib.Path(session.__file__).read_text()
    imports = [node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    assert imports == ["__future__", "dataclasses", "skippy.collaboration.github_issue_comment_reader"]
    assert "__main__" not in source
    result = run(Fake([response()]))
    assert result.observations == ()


class FakeWireResponse:
    def __init__(self, page, final_url):
        self.status = page.status
        self.url = final_url
        self.headers = Message()
        for name, value in page.headers.items():
            self.headers[name] = value
        self.stream = io.BytesIO(page.body)

    def read(self, amount):
        return self.stream.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url,
                           dict(request.header_items()), timeout))
        return self.responses[len(self.calls) - 1]


def test_real_transport_with_fake_opener_session_chain():
    opener = FakeOpener([FakeWireResponse(response([comment(1)], 2), url(1)),
                         FakeWireResponse(response([comment(2)]), url(2))])
    result = session.read_comment_session(https.ProductionHTTPTransport(opener),
                                          "SGGlobal/QLA", 23)
    assert result.evidence.outcome == "NO_NEXT_HINT"
    assert result.evidence.transport_calls == 2
    assert [o.comment_id for o in result.observations] == [1, 2]
    assert opener.calls == [("GET", url(p), {"Accept": "application/vnd.github+json"}, 10)
                            for p in [1, 2]]


@pytest.mark.parametrize("kind", ["redirect", "duplicate_header"])
def test_real_transport_later_fault_does_not_release_first_page(kind):
    second = FakeWireResponse(response([comment(2)]), url(2))
    if kind == "redirect":
        second.url = "https://evil.invalid/"
    else:
        second.headers["Content-Type"] = "application/json"
    opener = FakeOpener([FakeWireResponse(response([comment(1)], 2), url(1)), second])
    result = session.read_comment_session(https.ProductionHTTPTransport(opener),
                                          "SGGlobal/QLA", 23)
    assert result.evidence.outcome == "UNCERTAIN" and result.observations is None
    assert result.evidence.transport_calls == 2
    assert result.evidence.validation_code == "TRANSPORT" and len(opener.calls) == 2


def test_unchanged_reader_disable_flag_still_blocks(monkeypatch):
    monkeypatch.setattr(reader, "execution_enabled", True)
    result = run(Fake([]))
    assert result.evidence.outcome == "REFUSED" and result.observations is None


@pytest.mark.parametrize("body", ["\ud800", "\r\n", "\x00", "e\u0301"])
def test_json_body_value_is_preserved_exactly(body):
    result = run(Fake([response([comment(1, body)])]))
    assert result.observations[0].body == body
