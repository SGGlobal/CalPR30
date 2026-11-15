"""Synthetic pagination metadata only: no live request or credentials."""
import sys

import pytest

from skippy.collaboration import github_issue_comment_reader as reader
from skippy.collaboration import railway_comment_probe as cli
from skippy.collaboration.github_comment_probe_wrapper import observe_issue23_once
from skippy.collaboration.github_live_provider import HTTPResponse

NAMED = 'https://api.github.com/repos/SGGlobal/QLA/issues'
NUMERIC = 'https://api.github.com/repositories/1448604243/issues'


@pytest.mark.parametrize('number', [23, 26])
@pytest.mark.parametrize('base', [NAMED, NUMERIC])
@pytest.mark.parametrize('query', ['per_page=25&page=2', 'page=2&per_page=25'])
def test_response_alias_is_canonicalized_without_following(number, base, query):
    calls = []
    def transport(*args):
        calls.append(args)
        return HTTPResponse(200, b'[]', {
            'content-type': 'application/json',
            'link': f'<{base}/{number}/comments?{query}>; rel="next"',
        })
    page = reader.read_comment_page(transport, 'SGGlobal/QLA', number, strict_response=True)
    assert page.next_token == f'{NAMED}/{number}/comments?per_page=25&page=2'
    assert page.pagination == 'NEXT_OBSERVED'
    assert calls == [('GET', f'{NAMED}/{number}/comments?per_page=25&page=1',
                      {'Accept': 'application/vnd.github+json'}, 10)]


@pytest.mark.parametrize('url', [
    f'{NUMERIC}/23/comments?per_page=25&page=2',
    f'{NAMED}/23/comments?page=2&per_page=25',
])
def test_aliases_are_metadata_only_not_new_request_targets(url):
    calls = []
    with pytest.raises(reader.ReaderRefusal) as caught:
        reader.read_comment_page(lambda *args: calls.append(args), 'SGGlobal/QLA', 23, url)
    assert caught.value.calls == 0 and calls == []


@pytest.mark.parametrize('url', [
    f'{NUMERIC.replace("1448604243", "1448604244")}/23/comments?per_page=25&page=2',
    f'{NUMERIC.replace("1448604243", "01448604243")}/23/comments?per_page=25&page=2',
    f'{NUMERIC}/26/comments?per_page=25&page=2',
    f'{NAMED.replace("SGGlobal/QLA", "other/QLA")}/23/comments?per_page=25&page=2',
    f'{NUMERIC}/23/comments?per_page=25&page=2&extra=1',
    f'{NUMERIC}/23/comments?page=2&per_page=25&page=2',
    f'{NUMERIC}/23/comments?per_page=25&page=02',
    f'{NUMERIC}/23/comments?per_page=25&page=10000',
    f'{NUMERIC}/23/comments?per_page=25&page=2#fragment',
    f'{NUMERIC}/23/comments?per_page=25&page=%32',
    f'{NUMERIC}/23/comments?per_page=100&page=2',
    f'{NUMERIC}/23/comments?per_page=25&page=3',
    f'{NUMERIC.replace("https:", "http:")}/23/comments?per_page=25&page=2',
    f'{NUMERIC.replace("api.github.com", "api.github.com.evil")}/23/comments?per_page=25&page=2',
    f'{NUMERIC.replace("api.github.com", "user@api.github.com")}/23/comments?per_page=25&page=2',
    f'{NUMERIC.replace("api.github.com", "api.github.com:443")}/23/comments?per_page=25&page=2',
    f'{NUMERIC}/23/comments/?per_page=25&page=2',
    f'{NUMERIC}/23/comments?per_page=25&page=2\n',
])
def test_foreign_or_ambiguous_link_is_refused_after_one_call(url):
    calls = []
    def transport(*args):
        calls.append(args)
        return HTTPResponse(200, b'[]', {
            'content-type': 'application/json', 'link': f'<{url}>; rel="next"',
        })
    with pytest.raises(reader.ReaderRefusal) as caught:
        reader.read_comment_page(transport, 'SGGlobal/QLA', 23, strict_response=True)
    # Strict selected-header validation rejects controls before link parsing.
    expected = 'CONTENT_TYPE' if '\n' in url else 'PAGINATION'
    assert caught.value.validation_code == expected
    assert caught.value.calls == len(calls) == 1


@pytest.mark.parametrize('suffix', [
    f'<{NUMERIC}/23/comments?page=2&per_page=25>; rel="next"',
    f'<{NUMERIC}/23/comments?per_page=25&page=1>; rel="last"',
])
def test_alias_does_not_hide_duplicate_or_contradictory_relation(suffix):
    link = f'<{NAMED}/23/comments?per_page=25&page=2>; rel="next", {suffix}'
    with pytest.raises(reader.ReaderRefusal) as caught:
        reader.read_comment_page(lambda *args: HTTPResponse(200, b'[]', {'link': link}),
                                 'SGGlobal/QLA', 23)
    assert caught.value.validation_code == 'PAGINATION'


def test_wrapper_observes_mixed_aliases_with_one_credentialed_request():
    calls = []
    def transport(*args):
        calls.append(args)
        return HTTPResponse(200, b'[]', {
            'content-type': 'application/json',
            'link': f'<{NUMERIC}/23/comments?page=2&per_page=25>; rel="next", '
                    f'<{NAMED}/23/comments?per_page=25&page=3>; rel="last"',
        })
    summary = observe_issue23_once(transport, 'synthetic_token')
    assert summary.transport_calls == 1
    assert summary.next_token == f'{NAMED}/23/comments?per_page=25&page=2'
    assert len(calls) == 1
    assert calls[0][1] == f'{NAMED}/23/comments?per_page=25&page=1'
    assert calls[0][2]['Authorization'] == 'Bearer synthetic_token'


def test_launcher_retains_marker_and_refuses_restart_after_alias_response(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, 'argv', ['probe'])
    monkeypatch.setenv('QLA_PROBE_ARM', cli.ARM_VALUE)
    monkeypatch.setenv('QLA_PROBE_GITHUB_TOKEN', 'synthetic_token')
    monkeypatch.setenv('QLA_PROBE_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('RAILWAY_VOLUME_MOUNT_PATH', str(tmp_path))
    calls = []
    def transport(*args):
        calls.append(args)
        return HTTPResponse(200, b'[]', {
            'content-type': 'application/json',
            'link': f'<{NUMERIC}/23/comments?page=2&per_page=25>; rel="next"',
        })
    monkeypatch.setattr(cli, 'production_transport', lambda: transport)
    assert cli.main() == 0
    assert 'OBSERVED' in capsys.readouterr().out
    marker = tmp_path / cli.MARKER
    before = marker.stat()
    assert marker.read_bytes() == b'ATTEMPT_RESERVED\n'
    assert cli.main() == 2 and len(calls) == 1
    assert 'REFUSED' in capsys.readouterr().out
    assert marker.stat().st_ino == before.st_ino
    assert marker.read_bytes() == b'ATTEMPT_RESERVED\n'
