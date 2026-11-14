"""Offline only: injected transports and an injected production opener, no network."""
import json
import pytest
from skippy.collaboration import github_issue_comment_reader as reader
from skippy.collaboration.github_live_provider import HTTPResponse
from skippy.collaboration.github_http_transport import ProductionHTTPTransport

URL = 'https://api.github.com/repos/SGGlobal/QLA/issues/23/comments?per_page=25&page='
BODY = b'[ {"id": 1, "body": "original bytes"} ]'

class Transport:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else HTTPResponse(200, BODY, {})
        self.error = error
        self.calls = []
    def __call__(self, *args):
        self.calls.append(args)
        if self.error: raise self.error
        return self.response

def test_raw_bytes_and_explicit_next_call():
    t = Transport(HTTPResponse(200, BODY, {'Link': f'<{URL}2>; rel="next", <{URL}3>; rel="last"'}))
    first = reader.read_comment_page(t, 'SGGlobal/QLA', 23)
    assert first.body is BODY and first.next_token == URL+'2'
    assert first.pagination == 'NEXT_OBSERVED' and len(t.calls) == 1
    assert t.calls[0] == ('GET', URL+'1', {'Accept':'application/vnd.github+json'}, 10)
    t.response = HTTPResponse(200, b'[]', {})
    second = reader.read_comment_page(t, 'SGGlobal/QLA', 23, first.next_token)
    assert len(t.calls) == 2 and t.calls[1][1] == URL+'2'
    assert second.pagination == 'UNKNOWN'

@pytest.mark.parametrize('number', [23, 26])
def test_missing_link_never_means_exhausted(number):
    t = Transport(HTTPResponse(200, b'[]', {}))
    page = reader.read_comment_page(t, 'SGGlobal/QLA', number)
    assert page.pagination == 'UNKNOWN' and page.next_token is None
    assert len(t.calls) == 1 and f'/issues/{number}/comments?' in t.calls[0][1]

@pytest.mark.parametrize('repo,number,token', [
    ('other/repo',23,None), ('SGGlobal/QLA',True,None), ('SGGlobal/QLA',24,None),
    ('SGGlobal/QLA',23,URL+'0'), ('SGGlobal/QLA',23,URL+'01'),
    ('SGGlobal/QLA',23,URL+'2&x=1'), ('SGGlobal/QLA',26,URL+'2'),
    ('SGGlobal/QLA',23,'https://evil.example/?page=2'),
    ('SGGlobal/QLA',23,URL.replace('api.github.com','api.github.com.evil')+'2'),
    ('SGGlobal/QLA',23,URL+'2#x'), ('SGGlobal/QLA',23,7),
    ('SGGlobal/QLA',23,URL+'10000'),
])
def test_bad_input_calls_nothing(repo,number,token):
    t = Transport()
    with pytest.raises(reader.ReaderRefusal) as e: reader.read_comment_page(t,repo,number,token)
    assert e.value.calls == 0 and t.calls == []

@pytest.mark.parametrize('link', [
    '<https://evil.example/>; rel="next"', f'<{URL}2>; rel="next", <{URL}2>; rel="next"',
    f'<{URL}3>; rel="next"', f'<{URL}2>; rel="next"\r\n', '', 'x'*8193,
    f'<{URL}2>; rel="unknown"', f'<{URL}2>; rel="first"',
    f'<{URL}2>; rel="prev"', f'<{URL}2&token=secret>; rel="next"',
    f'<{URL}2>; rel="next", <{URL}1>; rel="last"',
    f'<{URL}1>; rel="last", <{URL}2>; rel="next"',
    f'<{URL.replace("/23/","/26/")}2>; rel="next"',
])
def test_bad_links_never_followed(link):
    t = Transport(HTTPResponse(200,BODY,{'Link':link}))
    with pytest.raises(reader.ReaderRefusal) as e: reader.read_comment_page(t,'SGGlobal/QLA',23)
    assert e.value.calls == 1 and len(t.calls) == 1

@pytest.mark.parametrize('response', [
    HTTPResponse(s,b'[]',{}) for s in [201,204,301,302,307,401,403,404,429,500,True]
] + [HTTPResponse(200,b'x'*1_000_001,{}),HTTPResponse(200,'[]',{}),None,
     HTTPResponse(200,BODY,{'Link':f'<{URL}2>; rel="next"','link':f'<{URL}2>; rel="next"'})])
def test_bad_responses_stop(response):
    t = Transport(); t.response = response
    with pytest.raises(reader.ReaderRefusal) as e: reader.read_comment_page(t,'SGGlobal/QLA',23)
    assert e.value.calls == 1 and len(t.calls) == 1

@pytest.mark.parametrize('body', [b'{',b'\xff',b'{}',b'[1]',b'[{"id":true,"body":"a"}]',
    b'[{"id":1,"id":2,"body":"a"}]',b'[{"id":1,"body":NaN}]',
    b'[{"id":1}]', b'[{"id":1,"body":"a"},{"id":1,"body":"b"}]',
    json.dumps([{'id':i+1,'body':None} for i in range(26)]).encode()])
def test_invalid_json_or_comment_page(body):
    t=Transport(HTTPResponse(200,body,{}))
    with pytest.raises(reader.ReaderRefusal): reader.read_comment_page(t,'SGGlobal/QLA',23)
    assert len(t.calls)==1

@pytest.mark.parametrize('error',[TimeoutError('secret'),RuntimeError('secret')])
def test_transport_errors_are_static_and_not_retried(error):
    t=Transport(error=error)
    with pytest.raises(reader.ReaderRefusal) as e: reader.read_comment_page(t,'SGGlobal/QLA',23)
    assert str(e.value)=='transport failed' and e.value.__suppress_context__
    assert e.value.calls==1 and len(t.calls)==1

def test_interrupt_propagates_without_retry():
    t=Transport(error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt): reader.read_comment_page(t,'SGGlobal/QLA',23)
    assert len(t.calls)==1

def test_execution_flag_and_non_callable_refused(monkeypatch):
    with pytest.raises(reader.ReaderRefusal) as e: reader.read_comment_page(None,'SGGlobal/QLA',23)
    assert e.value.calls==0
    monkeypatch.setattr(reader,'execution_enabled',True)
    t=Transport()
    with pytest.raises(reader.ReaderRefusal): reader.read_comment_page(t,'SGGlobal/QLA',23)
    assert t.calls==[]

def test_production_transport_preserves_link_without_automatic_follow():
    class Response:
        url=URL+'1'
        status=200
        headers={'Link':f'<{URL}2>; rel="next"'}
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n):
            body=getattr(self,'body',BODY); self.body=b''; return body
    class Opener:
        calls=0
        def open(self,request,timeout):
            self.calls+=1
            assert request.full_url==URL+'1' and request.get_method()=='GET' and timeout==10
            return Response()
    opener=Opener()
    page=reader.read_comment_page(ProductionHTTPTransport(opener),'SGGlobal/QLA',23)
    assert opener.calls==1 and page.body==BODY
    assert page.next_token == URL+'2' and page.pagination=='NEXT_OBSERVED'

# Independent review LOW notes: pin the offline boundary without widening it.
@pytest.mark.parametrize('token', [
    URL.replace('api.github.com', 'api.github.com.')+'2',
    URL.replace('api.github.com', 'user@api.github.com')+'2',
    URL.replace('api.github.com', '%61pi.github.com')+'2',
    URL.replace('per_page=25&page=', 'page=2&per_page=')+'25',
])
def test_review_url_edges_refuse_before_transport(token):
    transport = Transport()
    with pytest.raises(reader.ReaderRefusal) as exc:
        reader.read_comment_page(transport, 'SGGlobal/QLA', 23, token)
    assert exc.value.calls == 0 and transport.calls == []

@pytest.mark.parametrize('relation', ['next last', 'Next'])
def test_review_relation_edges_refuse_without_following(relation):
    transport = Transport(HTTPResponse(200, BODY, {'Link': f'<{URL}2>; rel="{relation}"'}))
    with pytest.raises(reader.ReaderRefusal) as exc:
        reader.read_comment_page(transport, 'SGGlobal/QLA', 23)
    assert exc.value.calls == 1
    assert transport.calls == [('GET', URL+'1', {'Accept': 'application/vnd.github+json'}, 10)]

@pytest.mark.parametrize('link', [
    f'<{URL}2>; rel="next", <{URL}2>; rel="last"',
    f'<{URL}2>; rel="last", <{URL}2>; rel="next"',
])
def test_review_coherent_next_last_pair_is_observed_only(link):
    transport = Transport(HTTPResponse(200, BODY, {'Link': link}))
    page = reader.read_comment_page(transport, 'SGGlobal/QLA', 23)
    assert page.next_token == URL+'2' and page.pagination == 'NEXT_OBSERVED'
    assert page.body is BODY
    assert transport.calls == [('GET', URL+'1', {'Accept': 'application/vnd.github+json'}, 10)]

def test_review_media_type_is_not_a_gate_in_offline_reader():
    # Deliberate current contract: decode returned bytes as UTF-8 JSON, do not
    # claim media-type enforcement or suitability for authenticated live use.
    transport = Transport(HTTPResponse(200, BODY, {'Content-Type': 'text/html; charset=iso-8859-1'}))
    page = reader.read_comment_page(transport, 'SGGlobal/QLA', 23)
    assert page.body is BODY and page.pagination == 'UNKNOWN'
    assert len(transport.calls) == 1

@pytest.mark.parametrize('value', [
    'application/json', 'application/vnd.github+json', 'APPLICATION/JSON',
    'application/json; charset=utf-8', 'application/vnd.github+json; charset="UTF-8"',
    ' application/json ; CHARSET = UTF-8 ',
])
def test_strict_content_type_accepts_utf8_json(value):
    transport=Transport(HTTPResponse(200,BODY,{'Content-Type':value}))
    result=reader.read_comment_page(transport,'SGGlobal/QLA',23,strict_response=True)
    assert result.body is BODY and result.pagination=='UNKNOWN'
    assert transport.calls == [('GET',URL+'1',{'Accept':'application/vnd.github+json'},10)]

@pytest.mark.parametrize('headers', [
    {}, {'Content-Type':'text/html'}, {'Content-Type':'application/json; charset=latin-1'},
    {'Content-Type':'application/json; charset=utf-8; charset=utf-8'},
    {'Content-Type':'application/json; version=1'}, {'Content-Type':'application/json;'},
    {'Content-Type':'application/json, application/json'},
    {'Content-Type':'application/json','content-type':'application/json'},
    {'Content-Type':'application/json\r\n'}, {'Content-Type':'application/json; charset="utf-8'},
    {'Content-Type':'application/json; charset="utf-8"x'},
    {'Content-Type':'application/json; charset=utf-8\t'},
    {'Content-Type':'application/json'+' '*16373},
])
def test_strict_response_refuses_bad_media_type_after_one_call(headers):
    transport=Transport(HTTPResponse(200,BODY,headers))
    with pytest.raises(reader.ReaderRefusal) as exc:
        reader.read_comment_page(transport,'SGGlobal/QLA',23,strict_response=True)
    assert exc.value.calls == 1
    assert transport.calls == [('GET',URL+'1',{'Accept':'application/vnd.github+json'},10)]

@pytest.mark.parametrize('option', [None,0,1,'yes'])
def test_strict_option_requires_boolean_before_call(option):
    transport=Transport()
    with pytest.raises(reader.ReaderRefusal) as exc:
        reader.read_comment_page(transport,'SGGlobal/QLA',23,strict_response=option)
    assert exc.value.calls==0 and transport.calls==[]

@pytest.mark.parametrize('link', [None, f'<{URL}2>; rel="next", <{URL}2>; rel="last"'])
def test_real_transport_to_strict_reader_with_fake_upstream(link):
    from email.message import Message
    from io import BytesIO
    calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append((request.get_method(),request.full_url,dict(request.header_items()),timeout))
            response=BytesIO(BODY)
            response.status=200
            response.url=URL+'1'
            response.headers=Message()
            response.headers['Content-Type']='application/json; charset=utf-8'
            if link: response.headers['Link']=link
            response.headers['Set-Cookie']='excluded'
            return response
    page=reader.read_comment_page(ProductionHTTPTransport(Opener()),'SGGlobal/QLA',23,strict_response=True)
    assert page.body==BODY
    assert page.pagination==('NEXT_OBSERVED' if link else 'UNKNOWN')
    assert page.next_token==(URL+'2' if link else None)
    assert calls==[('GET',URL+'1',{'Accept':'application/vnd.github+json'},10)]

@pytest.mark.parametrize('body', [b'not JSON', b'\xff', b'{}', b'[{"id":true,"body":"x"}]'])
def test_strict_media_type_does_not_replace_json_shape_checks(body):
    transport=Transport(HTTPResponse(200,body,{'Content-Type':'application/json'}))
    with pytest.raises(reader.ReaderRefusal) as exc:
        reader.read_comment_page(transport,'SGGlobal/QLA',23,strict_response=True)
    assert exc.value.calls==1 and len(transport.calls)==1

@pytest.mark.parametrize('link', [f'<{URL}3>; rel="next"', f'<{URL}2>; rel="next", <{URL}1>; rel="last"'])
def test_strict_fake_opener_bad_links_stop_after_original_request(link):
    from email.message import Message
    from io import BytesIO
    calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append((request.get_method(),request.full_url,dict(request.header_items()),timeout))
            response=BytesIO(BODY)
            response.status=200
            response.url=URL+'1'
            response.headers=Message()
            response.headers['Content-Type']='application/json'
            response.headers['Link']=link
            return response
    with pytest.raises(reader.ReaderRefusal) as exc:
        reader.read_comment_page(ProductionHTTPTransport(Opener()),'SGGlobal/QLA',23,strict_response=True)
    assert exc.value.calls==1
    assert calls==[('GET',URL+'1',{'Accept':'application/vnd.github+json'},10)]
