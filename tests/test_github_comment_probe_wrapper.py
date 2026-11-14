"""Synthetic credential tests only: no network, real token or secret-store access."""
import hashlib
from email.message import Message
from io import BytesIO
import pytest
from skippy.collaboration import github_comment_probe_wrapper as wrapper
from skippy.collaboration.github_live_provider import HTTPResponse
from skippy.collaboration.github_http_transport import ProductionHTTPTransport

TOKEN='synthetic_test_token_123'
URL=wrapper.PROBE_URL
BODY=b'[{"id":9,"body":"first"},{"id":3,"body":null}]'

class Fake:
    def __init__(self, response=None, error=None):
        self.calls=[]
        self.response=response if response is not None else HTTPResponse(200,BODY,{'Content-Type':'application/json'})
        self.error=error
    def __call__(self,*args):
        self.calls.append(args)
        if self.error: raise self.error
        return self.response

def test_one_strict_request_and_summary_only():
    fake=Fake()
    summary=wrapper.observe_issue23_once(fake,TOKEN)
    assert fake.calls==[('GET',URL,{'Accept':'application/vnd.github+json','Authorization':'Bearer '+TOKEN},10)]
    assert summary==wrapper.ProbeSummary(200,1,len(BODY),hashlib.sha256(BODY).hexdigest(),2,3,9,'UNKNOWN',None)
    assert TOKEN not in repr(summary) and 'first' not in repr(summary)
    assert not hasattr(summary,'body') and not hasattr(summary,'headers')

@pytest.mark.parametrize('token',[None,True,7,b'abc','','a b','Bearer abc','x\r\ny','x\x00','é','x\t','x'*513])
def test_bad_credential_refuses_before_delegate(token):
    fake=Fake()
    with pytest.raises(wrapper.ProbeRefusal) as exc: wrapper.observe_issue23_once(fake,token)
    assert str(exc.value)=='credential syntax refused' and exc.value.calls==0 and fake.calls==[]

def test_maximum_token_is_supported_without_echo():
    token='x'*512
    summary=wrapper.observe_issue23_once(Fake(),token)
    assert token not in repr(summary)

@pytest.mark.parametrize('status',[201,301,302,307,401,403,404,429,500])
def test_non200_reports_status_without_retry(status):
    fake=Fake(HTTPResponse(status,b'secret echo '+TOKEN.encode(),{}))
    with pytest.raises(wrapper.ProbeRefusal) as exc: wrapper.observe_issue23_once(fake,TOKEN)
    assert exc.value.calls==1 and exc.value.status==status
    assert str(exc.value)=='probe refused' and TOKEN not in repr(exc.value)
    assert len(fake.calls)==1

@pytest.mark.parametrize('error',[RuntimeError(TOKEN),TimeoutError(TOKEN),KeyboardInterrupt(TOKEN),SystemExit(TOKEN)])
def test_delegate_errors_and_cancellations_are_redacted_and_not_retried(error):
    fake=Fake(error=error)
    with pytest.raises(wrapper.ProbeRefusal) as exc: wrapper.observe_issue23_once(fake,TOKEN)
    assert exc.value.calls==1 and exc.value.status is None and len(fake.calls)==1
    assert TOKEN not in str(exc.value) and exc.value.__suppress_context__

@pytest.mark.parametrize('response',[
    HTTPResponse(200,BODY,{}),HTTPResponse(200,BODY,{'Content-Type':'text/html'}),
    HTTPResponse(200,b'bad json',{'Content-Type':'application/json'}),
    HTTPResponse(200,b'x'*1_000_001,{'Content-Type':'application/json'}),
    HTTPResponse(200,b'{}',{'Content-Type':'application/json'}),
    HTTPResponse(200,BODY,{'Content-Type':'application/json','Link':'<https://evil.example/>; rel="next"'}),
])
def test_strict_reader_refusals_preserve_one_call(response):
    fake=Fake(response)
    with pytest.raises(wrapper.ProbeRefusal) as exc: wrapper.observe_issue23_once(fake,TOKEN)
    assert exc.value.calls==1 and exc.value.status==200 and len(fake.calls)==1

@pytest.mark.parametrize('changed',[
    ('POST',URL,{'Accept':'application/vnd.github+json'},10),
    ('GET',URL.replace('/23/','/26/'),{'Accept':'application/vnd.github+json'},10),
    ('GET',URL.replace('page=1','page=2'),{'Accept':'application/vnd.github+json'},10),
    ('GET',URL,{'Accept':'application/vnd.github+json','Authorization':'other'},10),
    ('GET',URL,{'Accept':'application/vnd.github+json'},9),
])
def test_wrapper_checks_target_before_attaching_token(changed,monkeypatch):
    def altered_reader(transport,*args,**kwargs): return transport(*changed)
    monkeypatch.setattr(wrapper,'read_comment_page',altered_reader)
    fake=Fake()
    with pytest.raises(wrapper.ProbeRefusal) as exc: wrapper.observe_issue23_once(fake,TOKEN)
    assert exc.value.calls==0 and fake.calls==[]

def test_second_delegation_is_blocked_even_if_reader_changes(monkeypatch):
    original=wrapper.read_comment_page
    def repeated(transport,*args,**kwargs):
        original(transport,*args,**kwargs)
        return original(transport,*args,**kwargs)
    monkeypatch.setattr(wrapper,'read_comment_page',repeated)
    fake=Fake()
    with pytest.raises(wrapper.ProbeRefusal) as exc: wrapper.observe_issue23_once(fake,TOKEN)
    assert exc.value.calls==1 and len(fake.calls)==1

def test_echoed_token_is_not_returned_as_raw_content():
    body=('[{"id":1,"body":"'+TOKEN+'"}]').encode()
    summary=wrapper.observe_issue23_once(Fake(HTTPResponse(200,body,{'Content-Type':'application/json'})),TOKEN)
    assert TOKEN not in repr(summary) and summary.body_sha256==hashlib.sha256(body).hexdigest()

def test_empty_result_and_explicit_fresh_invocation():
    fake=Fake(HTTPResponse(200,b'[]',{'Content-Type':'application/json'}))
    summary=wrapper.observe_issue23_once(fake,TOKEN)
    assert summary.minimum_id is None and summary.maximum_id is None and summary.comment_count==0
    wrapper.observe_issue23_once(fake,TOKEN)
    assert len(fake.calls)==2 # Per invocation budget, not durable replay prevention.

def test_noncallable_and_execution_flag_refuse(monkeypatch):
    with pytest.raises(wrapper.ProbeRefusal) as exc: wrapper.observe_issue23_once(None,TOKEN)
    assert exc.value.calls==0
    monkeypatch.setattr(wrapper,'execution_enabled',True)
    fake=Fake()
    with pytest.raises(wrapper.ProbeRefusal) as exc: wrapper.observe_issue23_once(fake,TOKEN)
    assert exc.value.calls==0 and fake.calls==[]

def test_actual_transport_fake_opener_authorization_and_pagination():
    calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append((request.get_method(),request.full_url,dict(request.header_items()),timeout))
            response=BytesIO(BODY); response.url=URL; response.status=200; response.headers=Message()
            response.headers['Content-Type']='application/json; charset=utf-8'
            response.headers['Link']='<'+URL.replace('page=1','page=2')+'>; rel="next"'
            return response
    summary=wrapper.observe_issue23_once(ProductionHTTPTransport(Opener()),TOKEN)
    assert calls==[('GET',URL,{'Accept':'application/vnd.github+json','Authorization':'Bearer '+TOKEN},10)]
    assert summary.pagination=='NEXT_OBSERVED' and summary.next_token==URL.replace('page=1','page=2')
    assert summary.transport_calls==1 and TOKEN not in repr(summary)
